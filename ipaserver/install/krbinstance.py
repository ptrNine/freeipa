# Authors: Simo Sorce <ssorce@redhat.com>
#
# Copyright (C) 2007  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from __future__ import absolute_import
from __future__ import print_function

import shutil
import os
import pwd
import socket
import dbus

import dns.name

from ipaserver.install import service
from ipaserver.install import installutils
from ipapython import ipaldap
from ipapython import ipautil
from ipapython import kernel_keyring
from ipalib import api, errors
from ipalib.constants import ANON_USER
from ipalib.install import certmonger
from ipapython.ipa_log_manager import root_logger
from ipapython.dn import DN
from ipapython.dogtag import KDC_PROFILE

from ipaserver.install import replication
from ipaserver.install import ldapupdate

from ipaserver.install import certs
from ipaplatform.constants import constants
from ipaplatform.tasks import tasks
from ipaplatform.paths import paths

PKINIT_ENABLED = 'pkinitEnabled'


def get_pkinit_request_ca():
    """
    Return the certmonger CA name which is serving the PKINIT certificate
    request. If the certificate is not tracked by Certmonger, return None
    """
    pkinit_request_id = certmonger.get_request_id(
        {'cert-file': paths.KDC_CERT})

    if pkinit_request_id is None:
        return

    return certmonger.get_request_value(pkinit_request_id, 'ca-name')


def is_pkinit_enabled():
    """
    check whether PKINIT is enabled on the master by checking for the presence
    of KDC certificate and it's tracking CA
    """

    if os.path.exists(paths.KDC_CERT):
        pkinit_request_ca = get_pkinit_request_ca()

        if pkinit_request_ca != "SelfSign":
            return True

    return False


class KpasswdInstance(service.SimpleServiceInstance):
    def __init__(self):
        service.SimpleServiceInstance.__init__(self, "kadmin")

class KrbInstance(service.Service):
    def __init__(self, fstore=None):
        super(KrbInstance, self).__init__(
            "krb5kdc",
            service_desc="Kerberos KDC",
            fstore=fstore
        )
        self.fqdn = None
        self.realm = None
        self.domain = None
        self.host = None
        self.admin_password = None
        self.master_password = None
        self.suffix = None
        self.subject_base = None
        self.kdc_password = None
        self.sub_dict = None
        self.pkcs12_info = None
        self.master_fqdn = None
        self.config_pkinit = None

    suffix = ipautil.dn_attribute_property('_suffix')
    subject_base = ipautil.dn_attribute_property('_subject_base')

    def init_info(self, realm_name, host_name, setup_pkinit=False,
                  subject_base=None):
        self.fqdn = host_name
        self.realm = realm_name
        self.suffix = ipautil.realm_to_suffix(realm_name)
        self.subject_base = subject_base
        self.config_pkinit = setup_pkinit

    def get_realm_suffix(self):
        return DN(('cn', self.realm), ('cn', 'kerberos'), self.suffix)

    def move_service_to_host(self, principal):
        """
        Used to move a host/ service principal created by kadmin.local from
        cn=kerberos to reside under the host entry.
        """

        service_dn = DN(('krbprincipalname', principal), self.get_realm_suffix())
        service_entry = api.Backend.ldap2.get_entry(service_dn)
        api.Backend.ldap2.delete_entry(service_entry)

        # Create a host entry for this master
        host_dn = DN(
            ('fqdn', self.fqdn), ('cn', 'computers'), ('cn', 'accounts'),
            self.suffix)
        host_entry = api.Backend.ldap2.make_entry(
            host_dn,
            objectclass=[
               'top', 'ipaobject', 'nshost', 'ipahost', 'ipaservice',
               'pkiuser', 'krbprincipalaux', 'krbprincipal',
               'krbticketpolicyaux', 'ipasshhost'],
            krbextradata=service_entry['krbextradata'],
            krblastpwdchange=service_entry['krblastpwdchange'],
            krbprincipalname=service_entry['krbprincipalname'],
            krbcanonicalname=service_entry['krbcanonicalname'],
            krbprincipalkey=service_entry['krbprincipalkey'],
            serverhostname=[self.fqdn.split('.',1)[0]],
            cn=[self.fqdn],
            fqdn=[self.fqdn],
            ipauniqueid=['autogenerate'],
            managedby=[host_dn],
        )
        if 'krbpasswordexpiration' in service_entry:
            host_entry['krbpasswordexpiration'] = service_entry[
                'krbpasswordexpiration']
        if 'krbticketflags' in service_entry:
            host_entry['krbticketflags'] = service_entry['krbticketflags']
        api.Backend.ldap2.add_entry(host_entry)

        # Add the host to the ipaserver host group
        ld = ldapupdate.LDAPUpdate(ldapi=True)
        ld.update([os.path.join(paths.UPDATES_DIR,
                                '20-ipaservers_hostgroup.update')])

    def __common_setup(self, realm_name, host_name, domain_name, admin_password):
        self.fqdn = host_name
        self.realm = realm_name.upper()
        self.host = host_name.split(".")[0]
        self.ip = socket.getaddrinfo(host_name, None, socket.AF_UNSPEC, socket.SOCK_STREAM)[0][4][0]
        self.domain = domain_name
        self.suffix = ipautil.realm_to_suffix(self.realm)
        self.kdc_password = ipautil.ipa_generate_password()
        self.admin_password = admin_password
        self.dm_password = admin_password

        self.__setup_sub_dict()

        self.backup_state("running", self.is_running())
        try:
            self.stop()
        except Exception:
            # It could have been not running
            pass

    def __common_post_setup(self):
        self.step("creating anonymous principal", self.add_anonymous_principal)
        self.step("starting the KDC", self.__start_instance)
        self.step("configuring KDC to start on boot", self.__enable)

    def create_instance(self, realm_name, host_name, domain_name, admin_password, master_password, setup_pkinit=False, pkcs12_info=None, subject_base=None):
        self.master_password = master_password
        self.pkcs12_info = pkcs12_info
        self.subject_base = subject_base
        self.config_pkinit = setup_pkinit

        self.__common_setup(realm_name, host_name, domain_name, admin_password)

        self.step("adding kerberos container to the directory", self.__add_krb_container)
        self.step("configuring KDC", self.__configure_instance)
        self.step("initialize kerberos container", self.__init_ipa_kdb)
        self.step("adding default ACIs", self.__add_default_acis)
        self.step("creating a keytab for the directory", self.__create_ds_keytab)
        self.step("creating a keytab for the machine", self.__create_host_keytab)
        self.step("adding the password extension to the directory", self.__add_pwd_extop_module)

        self.__common_post_setup()

        self.start_creation()

        self.kpasswd = KpasswdInstance()
        self.kpasswd.create_instance('KPASSWD', self.fqdn, self.suffix,
                                     realm=self.realm)

    def create_replica(self, realm_name,
                       master_fqdn, host_name,
                       domain_name, admin_password,
                       setup_pkinit=False, pkcs12_info=None,
                       subject_base=None, promote=False):
        self.pkcs12_info = pkcs12_info
        self.subject_base = subject_base
        self.master_fqdn = master_fqdn
        self.config_pkinit = setup_pkinit

        self.__common_setup(realm_name, host_name, domain_name, admin_password)

        self.step("configuring KDC", self.__configure_instance)
        self.step("adding the password extension to the directory", self.__add_pwd_extop_module)

        self.__common_post_setup()

        self.start_creation()

        self.kpasswd = KpasswdInstance()
        self.kpasswd.create_instance('KPASSWD', self.fqdn, self.suffix)


    def __enable(self):
        self.backup_state("enabled", self.is_enabled())
        # We do not let the system start IPA components on its own,
        # Instead we reply on the IPA init script to start only enabled
        # components as found in our LDAP configuration tree
        self.ldap_enable('KDC', self.fqdn, None, self.suffix)

    def __start_instance(self):
        try:
            self.start()
        except Exception:
            root_logger.critical("krb5kdc service failed to start")

    def __setup_sub_dict(self):
        self.sub_dict = dict(FQDN=self.fqdn,
                             IP=self.ip,
                             PASSWORD=self.kdc_password,
                             SUFFIX=self.suffix,
                             DOMAIN=self.domain,
                             HOST=self.host,
                             SERVER_ID=installutils.realm_to_serverid(self.realm),
                             REALM=self.realm,
                             KRB5KDC_KADM5_ACL=paths.KRB5KDC_KADM5_ACL,
                             DICT_WORDS=paths.DICT_WORDS,
                             KRB5KDC_KADM5_KEYTAB=paths.KRB5KDC_KADM5_KEYTAB,
                             KDC_CERT=paths.KDC_CERT,
                             KDC_KEY=paths.KDC_KEY,
                             CACERT_PEM=paths.CACERT_PEM)

        # IPA server/KDC is not a subdomain of default domain
        # Proper domain-realm mapping needs to be specified
        domain = dns.name.from_text(self.domain)
        fqdn = dns.name.from_text(self.fqdn)
        if not fqdn.is_subdomain(domain):
            root_logger.debug("IPA FQDN '%s' is not located in default domain '%s'",
                    fqdn, domain)
            server_domain = fqdn.parent().to_unicode(omit_final_dot=True)
            root_logger.debug("Domain '%s' needs additional mapping in krb5.conf",
                server_domain)
            dr_map = " .%(domain)s = %(realm)s\n %(domain)s = %(realm)s\n" \
                        % dict(domain=server_domain, realm=self.realm)
        else:
            dr_map = ""
        self.sub_dict['OTHER_DOMAIN_REALM_MAPS'] = dr_map

        # Configure KEYRING CCACHE if supported
        if kernel_keyring.is_persistent_keyring_supported():
            root_logger.debug("Enabling persistent keyring CCACHE")
            self.sub_dict['OTHER_LIBDEFAULTS'] = \
                " default_ccache_name = KEYRING:persistent:%{uid}\n"
        else:
            root_logger.debug("Persistent keyring CCACHE is not enabled")
            self.sub_dict['OTHER_LIBDEFAULTS'] = ''

    def __add_krb_container(self):
        self._ldap_mod("kerberos.ldif", self.sub_dict)

    def __add_default_acis(self):
        self._ldap_mod("default-aci.ldif", self.sub_dict)

    def __template_file(self, path, chmod=0o644):
        template = os.path.join(paths.USR_SHARE_IPA_DIR,
                                os.path.basename(path) + ".template")
        conf = ipautil.template_file(template, self.sub_dict)
        self.fstore.backup_file(path)
        fd = open(path, "w+")
        fd.write(conf)
        fd.close()
        if chmod is not None:
            os.chmod(path, chmod)

    def __init_ipa_kdb(self):
        # kdb5_util may take a very long time when entropy is low
        installutils.check_entropy()

        #populate the directory with the realm structure
        args = ["kdb5_util", "create", "-s",
                                       "-r", self.realm,
                                       "-x", "ipa-setup-override-restrictions"]
        dialogue = (
            # Enter KDC database master key:
            self.master_password + '\n',
            # Re-enter KDC database master key to verify:
            self.master_password + '\n',
        )
        try:
            ipautil.run(args, nolog=(self.master_password,), stdin=''.join(dialogue))
        except ipautil.CalledProcessError:
            print("Failed to initialize the realm container")

    def __configure_instance(self):
        self.__template_file(paths.KRB5KDC_KDC_CONF, chmod=None)
        self.__template_file(paths.KRB5_CONF)
        self.__template_file(paths.HTML_KRB5_INI)
        self.__template_file(paths.KRB_CON)
        self.__template_file(paths.HTML_KRBREALM_CON)

        MIN_KRB5KDC_WITH_WORKERS = "1.9"
        cpus = os.sysconf('SC_NPROCESSORS_ONLN')
        workers = False
        result = ipautil.run(['klist', '-V'],
                             raiseonerr=False, capture_output=True)
        if result.returncode == 0:
            verstr = result.output.split()[-1]
            ver = tasks.parse_ipa_version(verstr)
            min = tasks.parse_ipa_version(MIN_KRB5KDC_WITH_WORKERS)
            if ver >= min:
                workers = True
        # Write down config file
        # We write realm and also number of workers (for multi-CPU systems)
        replacevars = {'KRB5REALM':self.realm}
        appendvars = {}
        if workers and cpus > 1:
            appendvars = {'KRB5KDC_ARGS': "'-w %s'" % str(cpus)}
        ipautil.backup_config_and_replace_variables(self.fstore, paths.SYSCONFIG_KRB5KDC_DIR,
                                                    replacevars=replacevars,
                                                    appendvars=appendvars)
        tasks.restore_context(paths.SYSCONFIG_KRB5KDC_DIR)

    #add the password extop module
    def __add_pwd_extop_module(self):
        self._ldap_mod("pwd-extop-conf.ldif", self.sub_dict)

    def __create_ds_keytab(self):
        ldap_principal = "ldap/" + self.fqdn + "@" + self.realm
        installutils.kadmin_addprinc(ldap_principal)
        self.move_service(ldap_principal)

        self.fstore.backup_file(paths.DS_KEYTAB)
        installutils.create_keytab(paths.DS_KEYTAB, ldap_principal)

        vardict = {"KRB5_KTNAME": paths.DS_KEYTAB}
        ipautil.config_replace_variables(paths.SYSCONFIG_DIRSRV,
                                         replacevars=vardict)
        pent = pwd.getpwnam(constants.DS_USER)
        os.chown(paths.DS_KEYTAB, pent.pw_uid, pent.pw_gid)

    def __create_host_keytab(self):
        host_principal = "host/" + self.fqdn + "@" + self.realm
        installutils.kadmin_addprinc(host_principal)

        self.fstore.backup_file(paths.KRB5_KEYTAB)
        installutils.create_keytab(paths.KRB5_KEYTAB, host_principal)

        # Make sure access is strictly reserved to root only for now
        os.chown(paths.KRB5_KEYTAB, 0, 0)
        os.chmod(paths.KRB5_KEYTAB, 0o600)

        self.move_service_to_host(host_principal)

    def _wait_for_replica_kdc_entry(self):
        master_dn = self.api.Object.server.get_dn(self.fqdn)
        kdc_dn = DN(('cn', 'KDC'), master_dn)

        ldap_uri = 'ldap://{}'.format(self.master_fqdn)

        with ipaldap.LDAPClient(
                ldap_uri, cacert=paths.IPA_CA_CRT) as remote_ldap:
            remote_ldap.gssapi_bind()
            replication.wait_for_entry(remote_ldap, kdc_dn, timeout=60)

    def _call_certmonger(self, certmonger_ca='IPA'):
        subject = str(DN(('cn', self.fqdn), self.subject_base))
        krbtgt = "krbtgt/" + self.realm + "@" + self.realm
        certpath = (paths.KDC_CERT, paths.KDC_KEY)

        try:
            prev_helper = None
            # on the first CA-ful master without '--no-pkinit', we issue the
            # certificate by contacting Dogtag directly
            use_dogtag_submit = all(
                [self.master_fqdn is None,
                 self.pkcs12_info is None,
                 self.config_pkinit])

            if use_dogtag_submit:
                ca_args = [
                    paths.CERTMONGER_DOGTAG_SUBMIT,
                    '--ee-url', 'https://%s:8443/ca/ee/ca' % self.fqdn,
                    '--certfile', paths.RA_AGENT_PEM,
                    '--keyfile', paths.RA_AGENT_KEY,
                    '--cafile', paths.IPA_CA_CRT,
                    '--agent-submit'
                ]
                helper = " ".join(ca_args)
                prev_helper = certmonger.modify_ca_helper(certmonger_ca, helper)

            certmonger.request_and_wait_for_cert(
                certpath,
                subject,
                krbtgt,
                ca=certmonger_ca,
                dns=self.fqdn,
                storage='FILE',
                profile=KDC_PROFILE)
        except dbus.DBusException as e:
            # if the certificate is already tracked, ignore the error
            name = e.get_dbus_name()
            if name != 'org.fedorahosted.certmonger.duplicate':
                root_logger.error("Failed to initiate the request: %s", e)
            return
        finally:
            if prev_helper is not None:
                certmonger.modify_ca_helper(certmonger_ca, prev_helper)

    def pkinit_enable(self):
        """
        advertise enabled PKINIT feature in master's KDC entry in LDAP
        """
        service.set_service_entry_config(
            'KDC', self.fqdn, [PKINIT_ENABLED], self.suffix)

    def issue_selfsigned_pkinit_certs(self):
        self._call_certmonger(certmonger_ca="SelfSign")
        # for self-signed certificate, the certificate is its own CA, copy it
        # as CA cert
        shutil.copyfile(paths.KDC_CERT, paths.CACERT_PEM)

    def issue_ipa_ca_signed_pkinit_certs(self):
        try:
            self._call_certmonger()
            # copy IPA CA bundle to the KDC's CA cert bundle
            shutil.copyfile(paths.IPA_CA_CRT, paths.CACERT_PEM)
            self.pkinit_enable()
        except RuntimeError as e:
            root_logger.error("PKINIT certificate request failed: %s", e)
            root_logger.error("Failed to configure PKINIT")
            self.stop_tracking_certs()
            self.issue_selfsigned_pkinit_certs()

    def install_external_pkinit_certs(self):
        certs.install_pem_from_p12(self.pkcs12_info[0],
                                   self.pkcs12_info[1],
                                   paths.KDC_CERT)
        certs.install_key_from_p12(self.pkcs12_info[0],
                                   self.pkcs12_info[1],
                                   paths.KDC_KEY)
        # copy IPA CA bundle to the KDC's CA cert bundle
        # NOTE: this may not be the same set of CA certificates trusted by
        # externally provided PKINIT cert.
        shutil.copyfile(paths.IPA_CA_CRT, paths.CACERT_PEM)
        self.pkinit_enable()

    def setup_pkinit(self):
        if self.pkcs12_info:
            self.install_external_pkinit_certs()
        elif self.config_pkinit:
            self.issue_ipa_ca_signed_pkinit_certs()

    def enable_ssl(self):
        """
        generate PKINIT certificate for KDC. If `--no-pkinit` was specified,
        only configure local self-signed KDC certificate for use as a FAST
        channel generator for WebUI. Do not advertise the installation steps in
        this case.
        """
        if self.master_fqdn is not None:
            self._wait_for_replica_kdc_entry()

        if self.config_pkinit:
            self.steps = []
            self.step("installing X509 Certificate for PKINIT",
                      self.setup_pkinit)
            self.start_creation()
        else:
            self.issue_selfsigned_pkinit_certs()

        try:
            self.restart()
        except Exception:
            root_logger.critical("krb5kdc service failed to restart")
            raise

    def get_anonymous_principal_name(self):
        return "%s@%s" % (ANON_USER, self.realm)

    def add_anonymous_principal(self):
        # Create the special anonymous principal
        princ_realm = self.get_anonymous_principal_name()
        dn = DN(('krbprincipalname', princ_realm), self.get_realm_suffix())
        try:
            self.api.Backend.ldap2.get_entry(dn)
        except errors.NotFound:
            installutils.kadmin_addprinc(princ_realm)
            self._ldap_mod("anon-princ-aci.ldif", self.sub_dict)

        try:
            self.api.Backend.ldap2.set_entry_active(dn, True)
        except errors.AlreadyActive:
            pass

    def __convert_to_gssapi_replication(self):
        repl = replication.ReplicationManager(self.realm,
                                              self.fqdn,
                                              self.dm_password)
        repl.convert_to_gssapi_replication(self.master_fqdn,
                                           r_binddn=DN(('cn', 'Directory Manager')),
                                           r_bindpw=self.dm_password)

    def stop_tracking_certs(self):
        certmonger.stop_tracking(certfile=paths.KDC_CERT)

    def uninstall(self):
        if self.is_configured():
            self.print_msg("Unconfiguring %s" % self.service_name)

        running = self.restore_state("running")
        enabled = self.restore_state("enabled")

        try:
            self.stop()
        except Exception:
            pass

        for f in [paths.KRB5KDC_KDC_CONF, paths.KRB5_CONF]:
            try:
                self.fstore.restore_file(f)
            except ValueError as error:
                root_logger.debug(error)

        # disabled by default, by ldap_enable()
        if enabled:
            self.enable()

        # stop tracking and remove certificates
        self.stop_tracking_certs()
        installutils.remove_file(paths.CACERT_PEM)
        installutils.remove_file(paths.KDC_CERT)
        installutils.remove_file(paths.KDC_KEY)

        if running:
            self.restart()

        self.kpasswd = KpasswdInstance()
        self.kpasswd.uninstall()
