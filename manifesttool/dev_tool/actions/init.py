# ----------------------------------------------------------------------------
# Copyright 2019 ARM Limited or its affiliates
#
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ----------------------------------------------------------------------------
import argparse
import datetime
import logging
import re
import time
import uuid
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from manifesttool import __version__
from manifesttool.dev_tool import defaults
from manifesttool.mtool import ecdsa_helper

SCRIPT_DIR = Path(__file__).resolve().parent

logger = logging.getLogger('manifest-dev-tool-init')


def _domain_factory(domain):
    if not re.match(r'^\S+\.\S+$', domain):
        raise argparse.ArgumentTypeError(
            'invalid vendor domain - {}'.format(domain))
    return domain


def register_parser(parser: argparse.ArgumentParser):
    parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Overwrite credentials and configuration files if found. '
             '[Default: False]'
    )

    parser.add_argument(
        '--cache-dir',
        help='Cache directory for preserving the state between "init" '
             'and "update" or "create" command invocations. '
             '[Default: {}]'.format(defaults.BASE_PATH),
        type=Path,
        default=defaults.BASE_PATH
    )

    service = parser.add_argument_group(
        'optional arguments (Pelion Device Management service configuration)')
    if defaults.PELION_GW:
        service.add_argument(
            '-p', '--gw-preset',
            help='API GW preset name specify GW URL and API key',
            choices=defaults.PELION_GW.keys()
        )
    service.add_argument(
        '-a', '--api-key',
        help='API key for for accessing Pelion Device Management service.'
    )
    service.add_argument(
        '-u', '--api-url',
        help='Pelion Device Management API gateway URL. '
    )

    parser.add_argument(
        '-g', '--generated-resource',
        help='Generated update resource C file. '
             '[Default: {}]'.format(defaults.UPDATE_RESOURCE_C),
        type=Path,
        default=defaults.UPDATE_RESOURCE_C
    )


def chunks(arr, num_of_elements):
    for i in range(0, len(arr), num_of_elements):
        yield arr[i:i + num_of_elements]


def format_rows(arr):
    for _slice in chunks(arr, 8):
        yield ', '.join(['0x{:02X}'.format(x) for x in _slice])


def pretty_print(arr):
    return ',\n    '.join(list(format_rows(arr)))


def generate_update_default_resources_c(
        c_source: Path,
        vendor_id: uuid.UUID,
        class_id: uuid.UUID,
        private_key_file: Path,
        certificate_file: Path,
        do_overwrite: bool
):
    """
    Generate update resources C source file for developer convenience.

    :param c_source: generated C source file
    :param vendor_id: vendor UUID
    :param class_id: class UUID
    :param private_key_file: private key file
    :param certificate_file: update certificate file
    :param do_overwrite: do overwrite existing file
    """

    if c_source.is_file() and not do_overwrite:
        logger.info('%s - exists', c_source)
        return

    vendor_id_str = pretty_print(vendor_id.bytes)
    class_id_str = pretty_print(class_id.bytes)

    cert_data = certificate_file.read_bytes()

    cert_str = pretty_print(cert_data)

    template = (SCRIPT_DIR / 'code_template.txt').read_text()

    public_key = ecdsa_helper.public_key_from_private(
        private_key_file.read_bytes())
    public_key_bytes = ecdsa_helper.public_key_to_bytes(public_key)
    update_public_key_str = pretty_print(public_key_bytes)

    c_source.write_text(
        template.format(
            vendor_id=vendor_id_str,
            class_id=class_id_str,
            cert=cert_str,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            tool='manifest-dev-tool init',
            version=__version__,
            update_pub_key=update_public_key_str
        )
    )
    logger.info('generated update source %s', c_source)


def generate_credentials(
        key_file,
        certificate_file,
        do_overwrite,
        cred_valid_time
):
    """
    Generate developer credentials

    :param key_file: Path - .pem signing key file
    :param certificate_file: Path - .der public key certificate file
    :param do_overwrite: bool - if True - overwrite existing credentials
    :param cred_valid_time: int - x.509 certificate validity period
    :return True when credentials were changed or generated, False otherwise
    """

    if not do_overwrite:
        if key_file.is_file() and certificate_file.is_file():
            logger.info('credentials exists')
            return False

        if key_file.is_file() != certificate_file.is_file():
            raise AssertionError(
                'Illegal state key pair is incomplete - execute again with '
                'force flag. \n'
                '\t{k_file} - {k_status}\n'
                '\t{c_file} - {c_status}'.format(
                    k_file=key_file.as_posix(),
                    k_status='ok' if key_file.is_file() else 'missing',
                    c_file=certificate_file.as_posix(),
                    c_status='ok' if certificate_file.is_file() else 'missing'
                )
            )
    try:
        logger.info('generating dev-credentials')
        key = ec.generate_private_key(ec.SECP256R1(), default_backend())

        # For a self-signed certificate the
        # subject and issuer are always the same.
        subject_list = [
            x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, 'localhost')]

        subject = issuer = x509.Name(subject_list)

        subject_key = key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo
        )
        hash_ctx = hashes.Hash(hashes.SHA256(), backend=default_backend())
        hash_ctx.update(subject_key)
        key_digest = hash_ctx.finalize()

        subject_key_identifier = key_digest[:160 // 8]  # Use RFC7093, Method 1

        cert = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            issuer
        ).public_key(
            key.public_key()
        ).serial_number(
            x509.random_serial_number()
        ).not_valid_before(
            datetime.datetime.utcnow()
        ).not_valid_after(
            datetime.datetime.utcnow() + datetime.timedelta(
                days=cred_valid_time)
        ).add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False),
            critical=False
        ).add_extension(
            x509.SubjectAlternativeName([x509.DNSName(u"localhost")]),
            critical=False,
        ).add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CODE_SIGNING]),
            critical=False,
        ).add_extension(
            x509.SubjectKeyIdentifier(subject_key_identifier),
            critical=False,
            # Sign our certificate with our private key
        ).sign(key, hashes.SHA256(), default_backend())

        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption()
            )
        )
        logger.info('created %s', key_file)

        certificate_file.parent.mkdir(parents=True, exist_ok=True)
        certificate_file.write_bytes(
            cert.public_bytes(serialization.Encoding.DER))

        logger.info('created %s', certificate_file)

        logger.info('dev-credentials - generated')

    except ValueError:
        logger.error('failed to generate dev-credentials')

        if key_file.is_file():
            key_file.unlink()
        if certificate_file.is_file():
            certificate_file.unlink()
        raise
    return True


def generate_developer_config(
        key_file: Path,
        certificate_file: Path,
        config,
        do_overwrite
):
    if config.is_file() and not do_overwrite:
        logger.info('developer config exists')
        with config.open() as fh:
            cfg_data = yaml.safe_load(fh)
        return (
            False,
            uuid.UUID(cfg_data['vendor-id']),
            uuid.UUID(cfg_data['class-id'])
        )

    vendor_id = uuid.uuid4()
    class_id = uuid.uuid4()

    cfg_data = {
        'key_file': key_file.as_posix(),
        'certificate': certificate_file.as_posix(),
        'class-id': class_id.bytes.hex(),
        'vendor-id': vendor_id.bytes.hex(),
    }

    config.parent.mkdir(parents=True, exist_ok=True)
    with config.open('wt') as fh:
        yaml.dump(cfg_data, fh)

    logger.info('generated developer config file %s', config)
    return True, vendor_id, class_id


def generate_service_config(api_key: str, api_url: str, api_config_path: Path):
    cfg = dict()

    if api_key is None and api_url is None:
        return

    if api_config_path.is_file():
        with api_config_path.open('rt') as fh:
            cfg = yaml.safe_load(fh)

    if api_key:
        cfg['api_key'] = api_key

    if api_url:
        cfg['host'] = api_url

    with api_config_path.open('wt') as fh:
        yaml.safe_dump(cfg, fh)


def entry_point(args):

    cache_dir = args.cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)

    is_credentials_updated = generate_credentials(
        key_file=cache_dir / defaults.UPDATE_PRIVATE_KEY,
        certificate_file=cache_dir / defaults.UPDATE_PUBLIC_KEY_CERT,
        do_overwrite=args.force,
        cred_valid_time=365 * 20  # years
    )

    is_cfg_updated, vendor_id, class_id = generate_developer_config(
        key_file=cache_dir / defaults.UPDATE_PRIVATE_KEY,
        certificate_file=cache_dir / defaults.UPDATE_PUBLIC_KEY_CERT,
        config=cache_dir / defaults.DEV_CFG,
        do_overwrite=args.force
    )

    generate_update_default_resources_c(
        c_source=args.generated_resource,
        vendor_id=vendor_id,
        class_id=class_id,
        private_key_file=cache_dir / defaults.UPDATE_PRIVATE_KEY,
        certificate_file=cache_dir / defaults.UPDATE_PUBLIC_KEY_CERT,
        do_overwrite=is_credentials_updated or is_cfg_updated
    )
    api_key = args.api_key
    if not api_key and hasattr(args, 'gw_preset') and args.gw_preset:
        api_key = defaults.PELION_GW[args.gw_preset].get('api_key')

    api_url = args.api_url
    if not api_url and hasattr(args, 'gw_preset') and args.gw_preset:
        api_url = defaults.PELION_GW[args.gw_preset].get('host')

    generate_service_config(
        api_key=api_key,
        api_url=api_url,
        api_config_path=cache_dir / defaults.CLOUD_CFG
    )

    cache_fw_version_file = cache_dir / defaults.UPDATE_VERSION
    if not cache_fw_version_file.is_file() or args.force:
        cache_fw_version_file.write_text('0.0.1')

    (cache_dir / defaults.DEV_README).write_text(
        'Files in this directory are autogenerated by "manifest-dev-tool init"'
        ' tool for internal use only.\nDo not modify.\n'
    )
