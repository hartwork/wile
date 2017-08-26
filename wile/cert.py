import atexit
import os
import re
import logging
import errno
from collections import OrderedDict
from datetime import datetime
from six import b
from six.moves import range

import click
import paramiko
from OpenSSL import crypto

from acme import challenges
from acme import messages
from acme import errors
from acme.jose.util import ComparableX509

from . import reg
from . import argtypes

logger = logging.getLogger('wile').getChild('cert')

# Taken from https://tools.ietf.org/html/rfc5280#section-5.3.1
# Not all all supported by letsencrypt's boulder.
REVOCATION_REASONS = OrderedDict((
    ('unspecified', 0),
    ('keyCompromise', 1),
    ('CACompromise', 2),
    ('affiliationChanged', 3),
    ('superseded', 4),
    ('cessationOfOperation', 5),
    ('certificateHold', 6),
    ('removeFromCRL', 8),
    ('privilegeWithdrawn', 9),
    ('AACompromise', 10),
))


@click.group()
def cert():
    '''
    Commands for certificate management.
    '''


@cert.command()
@click.pass_context
@click.option('--with-chain/--separate-chain', is_flag=True, default=True, show_default=False,
              help='Whether to include the certificate\'s chain in the output certificate; --separate-chain implies a '
                   'separate .chain.crt file, containing only the signing certificates up to the root '
                   '[default: with chain]')
@click.option('--key-size', '-s', metavar='SIZE', type=int, default=2048, show_default=True,
              help='Size in bits for the generated certificate\'s key')
@click.option('--output-dir', metavar='DIR', type=argtypes.WritablePathType, default='.',
              help='Where to store created certificates (default: current directory)')
@click.option('--basename', metavar='BASENAME',
              help='Basename to use when storing output: BASENAME.crt and BASENAME.key [default: first domain]')
@click.option('--key-digest', metavar='DIGEST', default='sha256', show_default=True,
              help='The digest to use when signing the request with its key (must be supported by openssl)')
@click.option('--min-valid-time', type=argtypes.TimespanType, metavar='TIMESPAN', default='25h', show_default=True,
              help='If a certificate is found and its expiration lies inside of this timespan, it will be '
                   'automatically requested and overwritten; otherwise no request will be made. The format for this '
                   'option is "1d" for one day. Supported units are hours, days and weeks.')
@click.option('--force', is_flag=True, default=False, show_default=True,
              help='Whether to force a request to be made, even if a valid certificate is found')
@click.option('--ssh-private-key', type=click.Path(file_okay=True, dir_okay=False), default=None, show_default=True,
              help='Path to SSH private key when using remote webroots. If not provided, the default paths will be '
                   'searched. If an SSH agent is running, it will also be queried independently of this setting.')
@click.argument('domainroots', 'DOMAIN[:WEBROOT]', type=argtypes.DomainWebrootType, metavar='DOMAIN[:WEBROOT]',
                nargs=-1, required=True)
def request(ctx, domainroots, with_chain, key_size, output_dir, basename,
            key_digest, min_valid_time, force, ssh_private_key):
    '''
    Request a new certificate for the provided domains and respective webroot paths.
    If a webroot is not provided for a domain, the one supplied for the previous domain is used.\n
    Each WEBROOT may be a local path or a remote path in the following format:
    [[[USER@]HOST[:PORT]:]PATH].\n
    In the second case an SSH connection will be made to the remote HOST and the ACME challanges will
    be remotely verified.
    '''

    regr = ctx.invoke(reg.register, quiet=True, auto_accept_tos=True)
    authzrs = list()

    domain_list, webroot_list = _generate_domain_and_webroot_lists_from_args(ctx, domainroots)
    basename = basename or domain_list[0]
    keyfile_path = os.path.join(output_dir, '%s.key' % basename)
    certfile_path = os.path.join(output_dir, '%s.crt' % basename)
    chainfile_path = os.path.join(output_dir, '%s.chain.crt' % basename)

    if os.path.exists(certfile_path):
        if not force and _is_valid_and_unchanged(certfile_path, domain_list, min_valid_time):
            logger.info('found existing valid certificate (%s); not requesting a new one', certfile_path)
            ctx.exit(0)
        elif force:
            logger.info('found existing valid certificate (%s), but forcing renewal on request', certfile_path)
        else:
            logger.info('existing certificate (%s) will expire inside of renewal time (%s) or has changes; '
                        'requesting new one', certfile_path, min_valid_time)
            force = True

    for (domain, webroot) in zip(domain_list, webroot_list):
        logger.info('requesting challenge for %s in %s', domain, webroot)

        authzr = ctx.obj.acme.request_domain_challenges(domain, new_authzr_uri=regr.new_authzr_uri)
        authzrs.append(authzr)

        challb = _get_http_challenge(ctx, authzr)
        chall_response, chall_validation = challb.response_and_validation(ctx.obj.account_key)
        _store_webroot_validation(ctx, webroot, ssh_private_key, challb,
                                  chall_validation)
        ctx.obj.acme.answer_challenge(challb, chall_response)

    key, csr = _generate_key_and_csr(domain_list, key_size, key_digest)

    try:
        crt, updated_authzrs = ctx.obj.acme.poll_and_request_issuance(csr, authzrs)
    except errors.PollError as e:
        if e.exhausted:
            logger.error('validation timed out for the following domains: %s', ', '.join(authzr.body.identifier for
                                                                                         authzr in e.exhausted))
        invalid_domains = [(e_authzr.body.identifier.value, _get_http_challenge(ctx, e_authzr).error.detail)
                           for e_authzr in e.updated.values() if e_authzr.body.status == messages.STATUS_INVALID]
        if invalid_domains:
            logger.error('validation invalid for the following domains:')
            for invalid_domain in invalid_domains:
                logger.error('%s: %s' % invalid_domain)
        ctx.exit(1)

    # write optional chain
    chain = ctx.obj.acme.fetch_chain(crt)
    certs = [crt.body]
    if with_chain:
        certs.extend(chain)
    else:
        if not force and os.path.exists(chainfile_path):
            _confirm_overwrite(chainfile_path)

        with open(chainfile_path, 'wb') as chainfile:
            for crt in chain:
                chainfile.write(crypto.dump_certificate(crypto.FILETYPE_PEM, crt))

    # write cert
    with open(certfile_path, 'wb') as certfile:
        for crt in certs:
            certfile.write(crypto.dump_certificate(crypto.FILETYPE_PEM, crt))

    # write key
    if not force and os.path.exists(keyfile_path):
        _confirm_overwrite(keyfile_path)

    with open(keyfile_path, 'wb') as keyfile:
        os.chmod(keyfile.name, 0o640)
        keyfile.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, key))


@cert.command()
@click.pass_context
@click.option('--reason', metavar='REASON', default='unspecified', type=click.Choice(REVOCATION_REASONS.keys()),
              show_default=True, help='reason for revoking certificate. Valid values: %s; not all are supported by '
                                      'Let\'s Encrypt' % REVOCATION_REASONS.keys())
@click.argument('cert_paths', metavar='CERT_FILE [CERT_FILE ...]', nargs=-1, required=True)
def revoke(ctx, reason, cert_paths):
    '''
    Revoke existing certificates.
    '''
    for cert_path in cert_paths:
        with open(cert_path, 'rb') as certfile:
            crt = crypto.load_certificate(crypto.FILETYPE_PEM, certfile.read())
            try:
                ctx.obj.acme.revoke(ComparableX509(crt), REVOCATION_REASONS[reason])
            except messages.Error as e:
                logger.error(e)


def _confirm_overwrite(filepath):
    click.confirm('file %s exists; overwrite?' % filepath, abort=True)


def _generate_domain_and_webroot_lists_from_args(ctx, domainroots):
    domain_list = list()
    webroot_list = list()
    webroot = None
    for domainroot in domainroots:
        if domainroot.webroot:
            if not len(domainroot.webroot.rsplit(':', 1)) > 1:
                webroot = argtypes.WritablePathType(domainroot.webroot)
            else:
                webroot = domainroot.webroot
        elif webroot:
            pass  # if we already have one from the last element, just use it
        else:
            logger.error('domain without webroot: %s', domainroot.domain)
            ctx.exit(1)
        domain_list.append(domainroot.domain)
        webroot_list.append(webroot)

    return (domain_list, webroot_list)


def _get_http_challenge(ctx, authzr):
    for combis in authzr.body.combinations:
        if len(combis) == 1 and isinstance(authzr.body.challenges[combis[0]].chall, challenges.HTTP01):
            return authzr.body.challenges[combis[0]]
    ctx.fail('no acceptable challenge type found; only HTTP01 supported')


def _store_webroot_validation(ctx, webroot, ssh_private_key, challb, val):
    logger.info('storing validation of %s', webroot)
    match = re.compile((r'^(?:(?:(?P<remote_user>[^@]+)@)?'
                        r'(?P<remote_host>[^@:]+)'
                        r'(?::(?P<remote_port>[0-9]+))?:)?'
                        r'(?P<webroot>[^:]+)$')).match(webroot)

    if not match:
        ctx.fail('could not parse %s as webroot' % webroot)

    remote_user = match.group('remote_user')
    remote_host = match.group('remote_host')
    remote_port = match.group('remote_port')
    remote_port = remote_port is None and 22 or int(remote_port)
    webroot = os.path.expanduser(match.group('webroot'))
    chall_path = os.path.join(webroot, challb.path.strip('/'))

    if not remote_host:
        webroot = argtypes.WritablePathType(webroot)
        try:
            os.makedirs(os.path.join(webroot, challb.URI_ROOT_PATH), 0o755)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise

        with open(chall_path, 'wb') as outf:
            logger.info('storing validation to %s', outf.name)
            outf.write(b(val))
            atexit.register(os.unlink, outf.name)
    else:
        try:
            ssh = paramiko.SSHClient()
            ssh.load_host_keys(os.path.expanduser('~/.ssh/known_hosts'))
            ssh.connect(hostname=remote_host, port=remote_port,
                        username=remote_user, key_filename=ssh_private_key,
                        password=os.getenv('WILE_SSH_PASS'))
            sftp = ssh.open_sftp()

            with sftp.open(chall_path, 'wb') as outf:
                logger.info('storing validation to %s' %
                            os.path.basename(chall_path))
                outf.write(b(val))

            sftp.close()
            ssh.close()
        except Exception as e:
            try:
                sftp.close()
                ssh.close()
            except Exception as e:
                pass
            ctx.fail('SFTP connection failed')


def _is_valid_and_unchanged(certfile_path, domains, min_valid_time):
    with open(certfile_path, 'rb') as certfile:
        crt = crypto.load_certificate(crypto.FILETYPE_PEM, certfile.read())
        # TODO: do we need to support the other possible ASN.1 date formats?
        expiration = datetime.strptime(crt.get_notAfter().decode('ascii'), '%Y%m%d%H%M%SZ')

        # create a set of domain names in the cert (DN + SANs)
        crt_domains = {dict(crt.get_subject().get_components())[b('CN')].decode('ascii')}
        for ext_idx in range(crt.get_extension_count()):
            ext = crt.get_extension(ext_idx)
            if ext.get_short_name() == b'subjectAltName':
                # we strip 'DNS:' without checking if it's there; if it
                # isn't, the cert uses some other unsupported identifier,
                # and is definitely different from the one we're creating
                crt_domains = crt_domains.union((x.strip()[4:] for x in str(ext).split(',')))

        if datetime.now() + min_valid_time > expiration:
            logger.info('EXPIRATION')
            return False
        elif crt_domains != set(domains):
            logger.info('DOMAINS: %s != %s', crt_domains, set(domains))
            return False
        else:
            return True


def _generate_key_and_csr(domains, key_size, key_digest):
    key = crypto.PKey()
    key.generate_key(crypto.TYPE_RSA, key_size)

    csr = crypto.X509Req()
    csr.set_version(2)
    csr.set_pubkey(key)

    sans = ', '.join('DNS:{}'.format(d) for d in domains)
    exts = [crypto.X509Extension(b'subjectAltName', False, b(sans))]
    csr.add_extensions(exts)

    csr.sign(key, str(key_digest))

    return (key, ComparableX509(csr))
