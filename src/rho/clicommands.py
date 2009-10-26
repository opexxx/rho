#
# Copyright (c) 2009 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#

""" Rho CLI Commands """

import csv
import os
import string
import sys


import gettext
t = gettext.translation('rho', 'locale', fallback=True)
_ = t.ugettext

from optparse import OptionParser
from getpass import getpass
import simplejson as json

from rho.log import log, setup_logging

from rho import config
from rho import crypto
from rho import scanner
from rho import ssh_jobs


RHO_PASSWORD = "RHO_PASSWORD"
RHO_AUTH_PASSWORD = "RHO_AUTH_PASSWORD"
DEFAULT_RHO_CONF = "~/.rho.conf"

def _read_key_file(filename):
    keyfile = open(os.path.expanduser(
        os.path.expandvars(filename)), "r")
    sshkey = keyfile.read()
    keyfile.close()
    return sshkey

def get_password(for_username, env_var_to_check):
    password = ""
    if env_var_to_check in os.environ:
        log.info("Using password from %s environment variable." %
                env_var_to_check)
        password = os.environ[env_var_to_check]
    else:
        password = getpass(_("Password for '%s':" % for_username))
    return password

def print_table(data):
    # +------+-------+------+
    # |      |       |      |
    # +------+-------+------+
    # |      |       |      |
    # +------+-------+------+

    # find the longest string in the column
    if not data:
        print()
        return

    table = "|"
    divider = "+"
    for k in data.keys():
        table += "%-10s |"
        divider += "%-10s +"

    print(table % tuple(data.keys()))

class CliCommand(object):
    """ Base class for all sub-commands. """

    def __init__(self, name="cli", usage=None, shortdesc=None,
            description=None):

        self.shortdesc = shortdesc
        if shortdesc is not None and description is None:
            description = shortdesc
        self.parser = OptionParser(usage=usage, description=description)
        self._add_common_options()
        self.name = name
        self.passphrase = None
        self.salt = None

    def _add_common_options(self):
        """ Add options that apply to all sub-commands. """
        self.parser.add_option("--debug", dest="debug",
                help=_("enable debug output"))

        # Default is expanded later:
        self.parser.add_option("--config", dest="config",
                help=_("config file name"), default=DEFAULT_RHO_CONF)

        self.parser.add_option("--log", dest="log_file", metavar="FILENAME",
                help=_("log file name (will be overwritten)"))
        self.parser.add_option("--log-level", dest="log_level",
                default="critical", metavar="LEVEL",
                help=_("log level (debug/info/warning/error/critical)"))

    def _validate_port(self, port):
        try:
            port_int = int(port)
        except ValueError:
            # aka, we get a string here...
            return False
        if int(port) < 1 or int(port) >65535:
            return False
        return True

    def _validate_ports(self, ports):
        # magic numbers, but this are valid tcp port ranges
        for port in ports:
            if not self._validate_port(port):
                print _("%s includes an invalid port number. Ports should be between 1 and 65535") % string.join(ports, ",")
                sys.exit(1)

    def _validate_options(self):
        """ 
        Sub-commands can override to do any argument validation they 
        require. 
        """
        pass

    def _do_command(self):
        pass

    def _read_config(self, filename, password):
        """
        Read config file and decrypt with the given password.

        Note that password here is the password provided by the user, not the
        actual salted AES key.
        """
        if os.path.exists(filename):
            (self.salt, confstr) = crypto.read_file(filename, password)
            try:
                return config.ConfigBuilder().build_config(confstr)
            except config.BadJsonException:
                print self.parser.error(_("Cannot parse configuration, check encryption password"))

        else:
            print _("Creating new config file: %s" % filename)
            # Need to generate a new salt as well:
            self.salt = os.urandom(8)
            return config.Config()

    def main(self):

        (self.options, self.args) = self.parser.parse_args()
        # we dont need argv[0] in this list...
        self.args = self.args[1:]

        # Setup logging, this must happen early!
        setup_logging(self.options.log_file, self.options.log_level)
        log.debug("Running cli command: %s" % self.name)

        # Translate path to config file to something absolute and expanded:
        self.options.config = os.path.abspath(os.path.expanduser(
            self.options.config))
        log.debug("Absolute config file: %s" % self.options.config)

        self._validate_options()

        if len(sys.argv) < 2:
            print(self.parser.error(_("Please enter at least 2 args")))

        if RHO_PASSWORD in os.environ:
            log.info("Using passphrase from %s environment variable." %
                    RHO_PASSWORD)
            self.passphrase = os.environ[RHO_PASSWORD]
        else:
            self.passphrase = getpass(_("Config Encryption Password:"))

        self.config = self._read_config(self.options.config, self.passphrase)

        # do the work, catch most common errors here:
        try:
            self._do_command()
        except config.DuplicateNameError, e:
            print _("ERROR: Name already exists: %s") % e.dupe_name
            sys.exit(1)
        except config.NoSuchAuthError, e:
            print _("ERROR: No such auth: %s") % e.authname
            sys.exit(1)


class ScanCommand(CliCommand):

    def __init__(self):
        usage = _("usage: %prog scan [options] PROFILE")
        shortdesc = _("scan given host profile")
        desc = _("scans the host profile")

        CliCommand.__init__(self, "scan", usage, shortdesc, desc)

        self.parser.add_option("--range", dest="ranges", action="append",
                metavar="RANGE", default=[],
                help=_("IP range to scan. See 'man rho' for supported formats."))

        self.parser.add_option("--ports", dest="ports", metavar="PORTS",
                help=_("list of ssh ports to try i.e. '22, 2222, 5402'"))
        self.parser.add_option("--username", dest="username",
                metavar="USERNAME",
                help=_("user name for authenticating against target machine"))
        self.parser.add_option("--auth", dest="auth", action="append",
                metavar="AUTH", default=[],
                help=_("auth class name to use"))
        self.parser.add_option("--output", dest="reportfile",
                metavar="REPORTFILE",
                help=_("write out to this file"))
        self.parser.add_option("--profile", dest="profiles", action="append",
               metavar="PROFILE", default=[],
               help=_("profile class to scan")),
        self.parser.add_option("--cache", dest="cachefile",
                metavar="PASTREPORTFILE",
                help=_("past output, used to cache successful credentials and ports"))

        self.parser.set_defaults(ports="22")

    def _validate_options(self):
        CliCommand._validate_options(self)

        # We support two ways to specify profiles, with --profile=blah, or
        # without --profile= and just list profiles in the command. Hack here
        # to treat those raw args as --profiles so the subsequent code has just
        # one path.
        self.options.profiles.extend(self.args)

        hasRanges = len(self.options.ranges) > 0
        hasProfiles = len(self.options.profiles) > 0
        hasAuths = len(self.options.auth) > 0

        if self.options.cachefile:
            self.options.cachefile = os.path.abspath(os.path.expanduser(
                self.options.cachefile))
            log.debug("Using cached output: %s" % self.options.cachefile)
            if not os.path.exists(self.options.cachefile):
                self.parser.error(_("No such file: %s" % self.options.cachefile))

        if not hasRanges and not hasProfiles:
            self.parser.print_help()
            sys.exit(1)

        if hasRanges and hasProfiles:
            self.parser.error(_("Cannot scan ranges and profiles at the same time."))

        if self.options.username and hasAuths:
            self.parser.error(_("Cannot specify both --username and --auth"))

        if hasRanges and not (self.options.username or hasAuths):
            self.parser.error(_(
                "--username or --auth required to scan a range."))

    def _build_cache(self, report_filename):
        """
        Reads in the results of a past report, parses them, and builds a dict
        such as:

        {
            "192.168.1.50": {'port': 22, 'authname': 'myauthname'},
        }
        """
        cache = {}
        f = open(report_filename)
        for row in csv.reader(f):

            if row[1] == '' or row[13] == '':
                # Looks like we couldn't login to this machine last time.
                continue

            # These indexes may be volatile if the output is fluctuating.
            ip = row[0]
            port = int(row[1])
            authname = row[13]
            cache[ip] = {'port': port, 'auth': authname}
            log.debug("Found cached results for: %s" % ip)

        f.close()
        return cache

    def _do_command(self):
        cache = {}
        if self.options.cachefile:
            cache = self._build_cache(self.options.cachefile)

        self.scanner = scanner.Scanner(config=self.config, cache=cache)

        # If username was specified, we need to prompt for a password
        # to go with it:
        user_password = ""
        if self.options.username:
            user_password = get_password(self.options.username,
                    RHO_AUTH_PASSWORD)

        if len(self.options.auth) > 0:
            auths = []
            for auth in self.options.auth:
                a = self.config.get_auth(auth)
                if a:
                    auths.append(a)
        else:
            # FIXME: need a more abstract credentials class -akl
            auth=config.SshAuth({'name':"clioptions",
                                        'username': self.options.username,
                                        'password': user_password,
                                        'type': 'ssh'})
            self.config.add_auth(auth)
            # if we are specifing auth stuff not in the config, add it to
            # the config class as "clioptions" and set the auth name to
            # the same
            self.options.auth = ["clioptions"]

        # this is all temporary, but make the tests pass
        if len(self.options.ranges) > 0:
            # create a temporary profile named "clioptions" for anything 
            # specified on the command line
            ports = []
            if self.options.ports:
                ports = self.options.ports.strip().split(",")
                self._validate_ports(ports)

            g = config.Profile(name="clioptions", ranges=self.options.ranges,
                         auth_names=self.options.auth, ports=ports)
            self.config.add_profile(g)
            self.scanner.scan_profiles(["clioptions"])
            
        if len(self.options.profiles) > 0:
            # seems like a lot of code to cat two possibly None lists...
            missing = self.scanner.scan_profiles(self.options.profiles)
            if missing:
                print _("The following profile names were not found:")
                for name in missing:
                    print name
        
        fileobj = sys.stdout
        if self.options.reportfile:
            fileobj = open(os.path.expanduser(os.path.expandvars(
                self.options.reportfile)), "w")
        self.scanner.report(fileobj)
        fileobj.close()


class DumpConfigCommand(CliCommand):
    """
    Dumps the config file to stdout.
    """

    def __init__(self):
        usage = _("usage: %prog dumpconfig [--config]")
        shortdesc = _("dumps the config file to stdout")
        desc = _("dumps the config file to stdout")

        CliCommand.__init__(self, "dumpconfig", usage, shortdesc, desc)

    def _validate_options(self):
        CliCommand._validate_options(self)

        if not os.path.exists(self.options.config):
            print _("No such file: %s" % self.options.config)
            sys.exit(1)

        if (not os.access(self.options.config, os.R_OK)):
            self.parser.print_help()
            sys.exit(1)


    def _do_command(self):
        """
        Executes the command.
        """
        (salt, content) = crypto.read_file(self.options.config, self.passphrase)
        print(json.dumps(json.loads(content), sort_keys = True, indent = 4))

        
class ImportConfigCommand(CliCommand):
    """
    Import a plaintext config file.
    """

    def __init__(self):
        usage = _("usage: %prog importconfig [--from-file]")
        shortdesc = _("import a plaintext config file")
        desc = _("imports a plaintext config file, encrypts it, and write to the destination config file.")

        CliCommand.__init__(self, "importconfig", usage, shortdesc, desc)

        self.parser.add_option("--from-file", dest="sourcefile", 
                metavar="FROMFILE",
                help=_("import configuration from raw json file"))

        # Generate a new salt as we're writing a new file here:
        self.salt = os.urandom(8)

    def _validate_options(self):
        CliCommand._validate_options(self)

        if not self.options.sourcefile:
            self.parser.error(_(
                "--from-file is required"))

        self.options.sourcefile = os.path.abspath(os.path.expanduser(
            self.options.sourcefile))

        # Ensure the source file exists:
        if not os.path.exists(self.options.sourcefile):
            self.parser.error(_("File does not exist") % 
                    self.options.sourcefile)

        # Make sure destination config file *doesn't* already exist, don't
        # want to accidentally overwrite config with this command.
        if os.path.exists(self.options.config):
            self.parser.error(_("Destination config file already exists: %s") %
                    self.options.config)

    def _do_command(self):
        """
        Executes the command.
        """
        f = open(self.options.sourcefile, 'r')
        json = f.read()
        imported_config = config.ConfigBuilder().build_config(json)
        c = config.ConfigBuilder().dump_config(imported_config)

        crypto.write_file(self.options.config, c, self.passphrase, self.salt)
        

class ProfileShowCommand(CliCommand):
    def __init__(self):
        usage = _("usage: %prog profile show [options]")
        shortdesc = _("show a network profile")
        desc = _("show a network profile")

        CliCommand.__init__(self, "profile show", usage, shortdesc, desc)

        self.parser.add_option("--name", dest="name", metavar="NAME",
                help=_("auth credential name - REQUIRED"))

    def _validate_options(self):
        CliCommand._validate_options(self)

        if not self.options.name:
            self.parser.print_help()
            sys.exit(1)

    def _do_command(self):
        if not self.config.list_profiles():
            print(_("No profiles found"))

        p = self.config.get_profile(self.options.name)
        if p:
            print(p.to_dict())
        else:
            print(_("No profile '%s' found.") % self.options.name)

class ProfileListCommand(CliCommand):
    def __init__(self):
        usage = _("usage: %prog profile list [options]")
        shortdesc = _("list the network profiles")
        desc = _("list the network profiles")

        CliCommand.__init__(self, "profile list", usage, shortdesc, desc)

    def _do_command(self):
        if not self.config.list_profiles():
            print(_("No profiles found"))

        for g in self.config.list_profiles():
            # make this a pretty table
            print(g.to_dict())


class AuthEditCommand(CliCommand):
    def __init__(self):
        usage = _("usage: %prog auth edit [options]")
        shortdesc = _("edits a given auth")
        desc = _("edit a given auth")

        CliCommand.__init__(self, "auth edit", usage, shortdesc, desc)

        self.parser.add_option("--name", dest="name", metavar="NAME",
                help=_("NAME of the auth - REQUIRED"))

        self.parser.add_option("--file", dest="filename", metavar="FILENAME",
                help=_("file containing SSH key"))
        self.parser.add_option("--username", dest="username",
                metavar="USERNAME",
                help=_("user name for authenticating against target machine - REQUIRED"))
        self.parser.add_option("--password", dest="password",
                metavar="PASSWORD",
                help=_("password for authenticating against target machine"))

        self.parser.set_defaults(password="")

    def _validate_options(self):
        CliCommand._validate_options(self)

        if not self.options.name:
            self.parser.print_help()
            sys.exit(1)

    def _do_command(self):
        a = self.config.get_auth(self.options.name)

        if not a:
            print(_("Auth %s does not exist.") % self.options.name)
            sys.exit(1)

        if self.options.username:
            a.username = self.options.username

        if self.options.password:
            a.password = self.options.password

        if self.options.filename:
            sshkey = _read_key_file(self.options.filename)

            if a.type == config.SSH_TYPE:
                cred = config.SshKeyAuth({"name": a.name,
                                          "key":sshkey,
                                          "username": a.username,
                                          "password": a.password,
                                          "type":"ssh_key"})
                # remove the old ssh, and new key type
                self.config.remove_auth(self.options.name)
                self.config.add_auth(cred)

            elif a.type == config.SSH_KEY_TYPE:
                a.key = sshkey

        c = config.ConfigBuilder().dump_config(self.config)
        crypto.write_file(self.options.config, c, self.passphrase, self.salt)
        print(_("Auth %s updated" % self.options.name))


class ProfileEditCommand(CliCommand):
    def __init__(self):
        usage = _("usage: %prog profile edit [options]")
        shortdesc = _("edits a given profile")
        desc = _("edit a given profile")

        CliCommand.__init__(self, "profile edit", usage, shortdesc, desc)

        self.parser.add_option("--name", dest="name", metavar="NAME",
                help=_("NAME of the profile - REQUIRED"))
        self.parser.add_option("--range", dest="ranges", action="append",
                metavar="RANGE", default=[],
                help=_("IP range to scan. See 'man rho' for supported formats."))

        self.parser.add_option("--ports", dest="ports", metavar="PORTS",
                help=_("list of ssh ports to try i.e. '22, 2222, 5402'")),
        self.parser.add_option("--auth", dest="auth", metavar="AUTH",
                action="append", default=[],
                help=_("auth class to associate with profile"))

        self.parser.set_defaults(ports="22")

    def _validate_options(self):
        CliCommand._validate_options(self)


        if not self.options.name:
            self.parser.print_help()
            sys.exit(1)

    def _do_command(self):
        g = self.config.get_profile(self.options.name)

        if not g:
            print(_("Profile %s does not exist.") % self.options.name)
            sys.exit(1)

        if self.options.ranges:
            g.ranges = self.options.ranges
        if self.options.ports:
            g.ports = self.options.ports.strip().split(",")
            self._validate_ports(g.ports)
        if len(self.options.auth) > 0:
            g.auth_names = self.options.auth

        c = config.ConfigBuilder().dump_config(self.config)
        crypto.write_file(self.options.config, c, self.passphrase, self.salt)
        print(_("Profile %s edited" % self.options.name))



class ProfileClearCommand(CliCommand):
    def __init__(self):
        usage = _("usage: %prog profile clear [--name | --all] [options]")
        shortdesc = _("removes 1 or all profiles from list")
        desc = _("removes profiles")

        CliCommand.__init__(self, "profile clear", usage, shortdesc, desc)

        self.parser.add_option("--name", dest="name", metavar="NAME",
                help=_("NAME of the profile to be removed"))
        self.parser.add_option("--all", dest="all", action="store_true",
                help=_("remove ALL profiles"))

        self.parser.set_defaults(all=False)

    def _validate_options(self):
        CliCommand._validate_options(self)

        if not self.options.name and not self.options.all:
            self.parser.print_help()
            sys.exit(1)

        if self.options.name and self.options.all:
            self.parser.print_help()
            sys.exit(1)

    def _do_command(self):
        if self.options.name:
            self.config.remove_profile(self.options.name)
            c = config.ConfigBuilder().dump_config(self.config)
            crypto.write_file(self.options.config, c, self.passphrase, self.salt)
            print(_("Profile %s removed" % self.options.name))
        elif self.options.all:
            self.config.clear_profiles()
            c = config.ConfigBuilder().dump_config(self.config)
            crypto.write_file(self.options.config, c, self.passphrase, self.salt)
            print(_("All network profiles removed"))

class ProfileAddCommand(CliCommand):
    def __init__(self):
        usage = _("usage: %prog profile add [options]")
        shortdesc = _("add a network profile")
        desc = _("add a network profile")

        CliCommand.__init__(self, "profile add", usage, shortdesc, desc)

        self.parser.add_option("--name", dest="name", metavar="NAME",
                help=_("NAME of the profile - REQUIRED"))
        self.parser.add_option("--range", dest="ranges", action="append",
                metavar="RANGE", default=[],
                help=_("IP range to scan. See 'man rho' for supported formats."))

        self.parser.add_option("--ports", dest="ports", metavar="PORTS",
                help=_("list of ssh ports to try i.e. '22, 2222, 5402'")),
        self.parser.add_option("--auth", dest="auth", metavar="AUTH",
                action="append", default=[],
                help=_("auth class to associate with profile"))

        self.parser.set_defaults(ports="22")

    def _validate_options(self):
        CliCommand._validate_options(self)

        if not self.options.name:
            self.parser.print_help()
            sys.exit(1)

    def _do_command(self):
        ports = []
        if self.options.ports:
            ports = self.options.ports.strip().split(",")
            self._validate_ports(ports)
            
        g = config.Profile(name=self.options.name, ranges=self.options.ranges,
                         auth_names=self.options.auth, ports=ports)
        self.config.add_profile(g)
        c = config.ConfigBuilder().dump_config(self.config)
        crypto.write_file(self.options.config, c, self.passphrase, self.salt)

class AuthClearCommand(CliCommand):
    def __init__(self):
        usage = _("usage: %prog auth clear")
        shortdesc = _("clears out the credentials")
        desc = _("clears out the crendentials")

        CliCommand.__init__(self, "auth clear", usage, shortdesc, desc)

        self.parser.add_option("--name", dest="name", metavar="NAME",
                help=_("NAME of the auth credential to be removed"))
        self.parser.add_option("--all", dest="all", action="store_true",
                help=_("remove ALL auth credentials"))

    def _validate_options(self):
        CliCommand._validate_options(self)

        if not self.options.name and not self.options.all:
            self.parser.print_help()
            sys.exit(1)

        if self.options.name and self.options.all:
            self.parser.print_help()
            sys.exit(1)

    def _do_command(self):
        if self.options.name:
            self.config.remove_auth(self.options.name)
        elif self.options.all:
            self.config.clear_auths()

        c = config.ConfigBuilder().dump_config(self.config)
        crypto.write_file(self.options.config, c, self.passphrase, self.salt)

class AuthShowCommand(CliCommand):
    def __init__(self):
        usage = _("usage: %prog auth show [options]")
        shortdesc = _("show auth credential")
        desc = _("show authentication crendential")

        CliCommand.__init__(self, "auth show", usage, shortdesc, desc)

        self.parser.add_option("--name", dest="name", metavar="NAME",
                help=_("auth credential name - REQUIRED"))
        self.parser.add_option("--showkeys", dest="keys", action="store_true",
                help=_("show ssh keys in the list"))

    def _validate_options(self):
        CliCommand._validate_options(self)

        if not self.options.name:
            self.parser.print_help()
            sys.exit(1)

    def _do_command(self):
        if not self.config.list_auths():
            print(_("No auth credentials found"))

        c = self.config.get_auth(self.options.name)
        if c:
            if c.type == "ssh_key" and not self.options.keys:
                c1 = dict(**c.to_dict())
                c1["password"] = "********"
                del c1["key"]
                print(c1)
            else:
                c["password"] = "********"
                print(c.to_dict())
        else:
            print(_("No auth '%s' found.") % self.options.name)

class AuthListCommand(CliCommand):
    def __init__(self):
        usage = _("usage: %prog auth list [options]")
        shortdesc = _("list auth credentials")
        desc = _("list authentication crendentials")

        CliCommand.__init__(self, "auth list", usage, shortdesc, desc)

        self.parser.add_option("--showkeys", dest="keys", action="store_true",
                help=_("show ssh keys in the list"))

    def _do_command(self):
        if not self.config.list_auths():
            print(_("No auth credentials found"))

        writer = csv.writer(sys.stdout, delimiter="\t")

        for c in self.config.list_auths():
            # make this a pretty table
            if c:
                if c.type == "ssh_key" and not self.options.keys:
                    c1 = dict(**c.to_dict())
                    del c1["key"]
                    writer.writerow(c1.items())
                else:
                    #print_table(c.to_dict())
                    writer.writerow(c.to_dict().items())

class AuthAddCommand(CliCommand):
    def __init__(self):
        usage = _("usage: %prog auth add [options]")
        shortdesc = _("add auth credentials to config")
        desc = _("adds the authorization credentials to the config")

        CliCommand.__init__(self, "auth add", usage, shortdesc, desc)

        self.parser.add_option("--name", dest="name", metavar="NAME",
                help=_("auth credential name - REQUIRED"))
        self.parser.add_option("--file", dest="filename", metavar="FILENAME",
                help=_("file containing SSH key"))
        self.parser.add_option("--username", dest="username",
                metavar="USERNAME",
                help=_("user name for authenticating against target machine - REQUIRED"))

    def _validate_options(self):
        CliCommand._validate_options(self)

        if not self.options.name:
            self.parser.print_help()
            sys.exit(1)

        # need to pass in file or username:
        if not self.options.username:
            self.parser.print_help()
            sys.exit(1)

    def _save_cred(self, cred):
        self.config.add_auth(cred)
        c = config.ConfigBuilder().dump_config(self.config)
        crypto.write_file(self.options.config, c, self.passphrase, self.salt)
        
    def _do_command(self):

        auth_password = get_password(self.options.username,
                RHO_AUTH_PASSWORD)

        if self.options.filename:
            # using sshkey
            sshkey = _read_key_file(self.options.filename)

            cred = config.SshKeyAuth({"name": self.options.name,
                "key": sshkey,
                "username": self.options.username,
                "password": auth_password,
                "type": "ssh_key"})

            self._save_cred(cred)


        elif self.options.username:
            # using ssh
            cred = config.SshAuth({"name":self.options.name,
                "username": self.options.username,
                "password": auth_password,
                "type": "ssh"})
            self._save_cred(cred)
