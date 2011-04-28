#
# installtree.py
#
# Copyright (C) 2010  Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
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
# Red Hat Author(s):  Martin Gracik <mgracik@redhat.com>
#

import logging
logger = logging.getLogger("pylorax.installtree")

import sys
import os
import shutil
import gzip
import re
import glob
import time
import subprocess
import operator

from base import BaseLoraxClass, DataHolder
import constants
from sysutils import *


class LoraxInstallTree(BaseLoraxClass):

    def __init__(self, yum, libdir):
        BaseLoraxClass.__init__(self)
        self.yum = yum
        self.root = self.yum.installroot
        self.libdir = libdir

        self.lcmds = constants.LoraxRequiredCommands()

    def remove_locales(self):
        chroot = lambda: os.chroot(self.root)

        # get locales we need to keep
        langtable = joinpaths(self.root, "usr/share/anaconda/lang-table")
        if not os.path.exists(langtable):
            logger.critical("could not find anaconda lang-table, exiting")
            sys.exit(1)

        # remove unneeded locales from /usr/share/locale
        with open(langtable, "r") as fobj:
            langs = fobj.readlines()

        langs = map(lambda l: l.split()[1], langs)

        localedir = joinpaths(self.root, "usr/share/locale")
        for fname in os.listdir(localedir):
            fpath = joinpaths(localedir, fname)
            if os.path.isdir(fpath) and fname not in langs:
                shutil.rmtree(fpath)

        # move the lang-table to etc
        shutil.move(langtable, joinpaths(self.root, "etc"))

    def create_keymaps(self, basearch):
        keymaps = joinpaths(self.root, "etc/keymaps.gz")

        # look for override
        override = "keymaps-override-{0}".format(basearch)
        override = joinpaths(self.root, "usr/share/anaconda", override)
        if os.path.isfile(override):
            logger.debug("using keymaps override")
            shutil.move(override, keymaps)
        else:
            # create keymaps
            cmd = [joinpaths(self.root, "usr/libexec/anaconda", "getkeymaps"),
                   basearch, keymaps, self.root]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
            proc.wait()

        return True

    def create_screenfont(self, basearch):
        dst = joinpaths(self.root, "etc/screenfont.gz")

        screenfont = "screenfont-{0}.gz".format(basearch)
        screenfont = joinpaths(self.root, "usr/share/anaconda", screenfont)
        if not os.path.isfile(screenfont):
            return False
        else:
            shutil.move(screenfont, dst)

        return True

    def move_stubs(self):
        stubs = ("list-harddrives", "loadkeys", "mknod",
                 "raidstart", "raidstop")

        for stub in stubs:
            src = joinpaths(self.root, "usr/share/anaconda",
                            "{0}-stub".format(stub))
            dst = joinpaths(self.root, "usr/bin", stub)
            if os.path.isfile(src):
                shutil.move(src, dst)

        # move restart-anaconda
        src = joinpaths(self.root, "usr/share/anaconda", "restart-anaconda")
        dst = joinpaths(self.root, "usr/bin")
        shutil.move(src, dst)

        # move sitecustomize.py
        pythonpath = joinpaths(self.root, "usr", self.libdir, "python?.?")
        for path in glob.glob(pythonpath):
            src = joinpaths(path, "site-packages/pyanaconda/sitecustomize.py")
            dst = joinpaths(path, "site-packages")
            shutil.move(src, dst)

    def remove_packages(self, remove):
        rdb = {}
        order = []
        for item in remove:
            package = None
            pattern = None

            if item[0] == "--path":
                # remove files
                package = None
                pattern = item[1]
            else:
                # remove package
                package = item[0]

                try:
                    pattern = item[1]
                except IndexError:
                    pattern = None

            if package not in rdb:
                rdb[package] = [pattern]
                order.append(package)
            elif pattern not in rdb[package]:
                rdb[package].append(pattern)

        for package in order:
            pattern_list = rdb[package]
            logger.debug("{0}\t{1}".format(package, pattern_list))
            self.yum.remove(package, pattern_list)

    def cleanup_python_files(self):
        for root, _, fnames in os.walk(self.root):
            for fname in fnames:
                if fname.endswith(".py"):
                    path = joinpaths(root, fname, follow_symlinks=False)
                    pyo, pyc = path + "o", path + "c"
                    if os.path.isfile(pyo):
                        os.unlink(pyo)
                    if os.path.isfile(pyc):
                        os.unlink(pyc)

                    os.symlink("/dev/null", pyc)

    def move_modules(self):
        shutil.move(joinpaths(self.root, "lib/modules"),
                    joinpaths(self.root, "modules"))
        shutil.move(joinpaths(self.root, "lib/firmware"),
                    joinpaths(self.root, "firmware"))

        os.symlink("../modules", joinpaths(self.root, "lib/modules"))
        os.symlink("../firmware", joinpaths(self.root, "lib/firmware"))

    def cleanup_kernel_modules(self, keepmodules, kernelver):
        logger.info("cleaning up kernel modules for %s", kernelver)
        moddir = joinpaths(self.root, "modules", kernelver)
        fwdir = joinpaths(self.root, "firmware")

        # expand required modules
        modules = set()
        pattern = re.compile(r"\.ko$")

        for name in keepmodules:
            if name.startswith("="):
                group = name[1:]
                if group in ("scsi", "ata"):
                    mpath = joinpaths(moddir, "modules.block")
                elif group == "net":
                    mpath = joinpaths(moddir, "modules.networking")
                else:
                    mpath = joinpaths(moddir, "modules.{0}".format(group))

                if os.path.isfile(mpath):
                    with open(mpath, "r") as fobj:
                        for line in fobj:
                            module = pattern.sub("", line.strip())
                            modules.add(module)
            else:
                modules.add(name)

        # resolve modules dependencies
        moddep = joinpaths(moddir, "modules.dep")
        with open(moddep, "r") as fobj:
            lines = map(lambda line: line.strip(), fobj.readlines())

        modpattern = re.compile(r"^.*/(?P<name>.*)\.ko:(?P<deps>.*)$")
        deppattern = re.compile(r"^.*/(?P<name>.*)\.ko$")
        unresolved = True

        while unresolved:
            unresolved = False
            for line in lines:
                match = modpattern.match(line)
                modname = match.group("name")
                if modname in modules:
                    # add the dependencies
                    for dep in match.group("deps").split():
                        match = deppattern.match(dep)
                        depname = match.group("name")
                        if depname not in modules:
                            unresolved = True
                            modules.add(depname)

        # required firmware
        firmware = set()
        firmware.add("atmel_at76c504c-wpa.bin")
        firmware.add("iwlwifi-3945-1.ucode")
        firmware.add("iwlwifi-3945.ucode")
        firmware.add("zd1211/zd1211_uph")
        firmware.add("zd1211/zd1211_uphm")
        firmware.add("zd1211/zd1211b_uph")
        firmware.add("zd1211/zd1211b_uphm")

        # remove not needed modules
        for root, _, fnames in os.walk(moddir):
            for fname in fnames:
                path = os.path.join(root, fname)
                name, ext = os.path.splitext(fname)

                if ext == ".ko":
                    if name not in modules:
                        os.unlink(path)
                        logger.debug("removed module {0}".format(path))
                    else:
                        # get the required firmware
                        cmd = [self.lcmds.MODINFO, "-F", "firmware", path]
                        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
                        output = proc.stdout.read()
                        firmware |= set(output.split())

        # remove not needed firmware
        firmware = map(lambda fw: joinpaths(fwdir, fw), list(firmware))
        for root, _, fnames in os.walk(fwdir):
            for fname in fnames:
                path = joinpaths(root, fname)
                if path not in firmware:
                    os.unlink(path)
                    logger.debug("removed firmware {0}".format(path))

        # get the modules paths
        modpaths = {}
        for root, _, fnames in os.walk(moddir):
            for fname in fnames:
                modpaths[fname] = joinpaths(root, fname)

        # create the modules list
        modlist = {}
        for modtype, fname in (("scsi", "modules.block"),
                               ("eth", "modules.networking")):

            fname = joinpaths(moddir, fname)
            with open(fname, "r") as fobj:
                lines = map(lambda l: l.strip(), fobj.readlines())
                lines = filter(lambda l: l, lines)

            for line in lines:
                modname, ext = os.path.splitext(line)
                if (line not in modpaths or
                    modname in ("floppy", "libiscsi", "scsi_mod")):
                    continue

                cmd = [self.lcmds.MODINFO, "-F", "description", modpaths[line]]
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
                output = proc.stdout.read()

                try:
                    desc = output.splitlines()[0]
                    desc = desc.strip()[:65]
                except IndexError:
                    desc = "{0} driver".format(modname)

                info = '{0}\n\t{1}\n\t"{2}"\n'
                info = info.format(modname, modtype, desc)
                modlist[modname] = info

        # write the module-info
        moduleinfo = joinpaths(os.path.dirname(moddir), "module-info")
        with open(moduleinfo, "w") as fobj:
            fobj.write("Version 0\n")
            for modname in sorted(modlist.keys()):
                fobj.write(modlist[modname])

    def compress_modules(self, kernelver):
        logger.debug("compressing modules for %s", kernelver)
        moddir = joinpaths(self.root, "modules", kernelver)

        for root, _, fnames in os.walk(moddir):
            for fname in filter(lambda f: f.endswith(".ko"), fnames):
                path = os.path.join(root, fname)
                with open(path, "rb") as fobj:
                    data = fobj.read()

                gzipped = gzip.open("{0}.gz".format(path), "wb")
                gzipped.write(data)
                gzipped.close()

                os.unlink(path)

    def run_depmod(self, kernelver):
        logger.debug("running depmod for %s", kernelver)
        systemmap = "System.map-{0}".format(kernelver)
        systemmap = joinpaths(self.root, "boot", systemmap)

        cmd = [self.lcmds.DEPMOD, "-a", "-F", systemmap, "-b", self.root,
               kernelver]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        retcode = proc.wait()
        if not retcode == 0:
            logger.critical(proc.stdout.read())
            sys.exit(1)

        moddir = joinpaths(self.root, "modules", kernelver)

        # remove *map files
        mapfiles = joinpaths(moddir, "*map")
        for fpath in glob.glob(mapfiles):
            os.unlink(fpath)

        # remove build and source symlinks
        for fname in ["build", "source"]:
            os.unlink(joinpaths(moddir, fname))

    def move_repos(self):
        src = joinpaths(self.root, "etc/yum.repos.d")
        dst = joinpaths(self.root, "etc/anaconda.repos.d")
        shutil.move(src, dst)

    def create_depmod_conf(self):
        text = "search updates built-in\n"

        with open(joinpaths(self.root, "etc/depmod.d/dd.conf"), "w") as fobj:
            fobj.write(text)

    def misc_s390_modifications(self):
        # copy linuxrc.s390
        src = joinpaths(self.root, "usr/share/anaconda/linuxrc.s390")
        dst = joinpaths(self.root, "sbin", "init")
        os.unlink(dst)
        shutil.copy2(src, dst)

    def misc_tree_modifications(self):
        # init symlinks
        target = "/sbin/init"
        name = joinpaths(self.root, "init")
        os.symlink(target, name)

        os.unlink(joinpaths(self.root, "etc/systemd/system/default.target"))
        os.symlink("/lib/systemd/system/anaconda.target", joinpaths(self.root, "etc/systemd/system/default.target"))

        # create resolv.conf
        touch(joinpaths(self.root, "etc", "resolv.conf"))

        # create a basic /bin/login script that'll automatically start up
        # bash as a login shell.  This is needed because we can't pass bash
        # arguments from the agetty command line, and there's not really a
        # better way to autologin root.
        with open(joinpaths(self.root, "bin/login"), "w") as fobj:
            fobj.write("#!/bin/bash\n")
            fobj.write("exec -l /bin/bash\n")

    def get_config_files(self, src_dir):
        # anaconda needs to change a couple of the default gconf entries
        gconf = joinpaths(self.root, "etc", "gconf", "gconf.xml.defaults")

        # 0 - path, 1 - entry type, 2 - value
        gconf_settings = \
        [("/apps/metacity/general/button_layout", "string", ":"),
         ("/apps/metacity/general/action_right_click_titlebar",
          "string", "none"),
         ("/apps/metacity/general/num_workspaces", "int", "1"),
         ("/apps/metacity/window_keybindings/close", "string", "disabled"),
         ("/apps/metacity/global_keybindings/run_command_window_screenshot",
          "string", "disabled"),
         ("/apps/metacity/global_keybindings/run_command_screenshot",
          "string", "disabled"),
         ("/apps/metacity/global_keybindings/switch_to_workspace_down",
          "string", "disabled"),
         ("/apps/metacity/global_keybindings/switch_to_workspace_left",
          "string", "disabled"),
         ("/apps/metacity/global_keybindings/switch_to_workspace_right",
          "string", "disabled"),
         ("/apps/metacity/global_keybindings/switch_to_workspace_up",
          "string", "disabled"),
         ("/desktop/gnome/interface/accessibility", "bool", "true"),
         ("/desktop/gnome/interface/at-spi-corba", "bool", "true")]

        for path, entry_type, value in gconf_settings:
            cmd = [self.lcmds.GCONFTOOL, "--direct",
                   "--config-source=xml:readwrite:{0}".format(gconf),
                   "-s", "-t", entry_type, path, value]

            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
            proc.wait()

        # get rsyslog config
        src = joinpaths(src_dir, "rsyslog.conf")
        dst = joinpaths(self.root, "etc")
        shutil.copy2(src, dst)

        # get .bash_history
        src = joinpaths(src_dir, ".bash_history")
        dst = joinpaths(self.root, "root")
        shutil.copy2(src, dst)

        # get .profile
        src = joinpaths(src_dir, ".profile")
        dst = joinpaths(self.root, "root")
        shutil.copy2(src, dst)

        # get libuser.conf
        src = joinpaths(src_dir, "libuser.conf")
        dst = joinpaths(self.root, "etc")
        shutil.copy2(src, dst)

        # get selinux config
        if os.path.exists(joinpaths(self.root, "etc/selinux/targeted")):
            src = joinpaths(src_dir, "selinux.config")
            dst = joinpaths(self.root, "etc/selinux", "config")
            shutil.copy2(src, dst)

        # get sysconfig files
        src = joinpaths(src_dir, "network")
        dst = joinpaths(self.root, "etc/sysconfig")
        shutil.copy2(src, dst)

    def setup_sshd(self, src_dir):
        # get sshd config
        src = joinpaths(src_dir, "sshd_config.anaconda")
        dst = joinpaths(self.root, "etc", "ssh")
        shutil.copy2(src, dst)

        src = joinpaths(src_dir, "pam.sshd")
        dst = joinpaths(self.root, "etc", "pam.d", "sshd")
        shutil.copy2(src, dst)

        dst = joinpaths(self.root, "etc", "pam.d", "login")
        shutil.copy2(src, dst)

        dst = joinpaths(self.root, "etc", "pam.d", "remote")
        shutil.copy2(src, dst)

        # enable root shell logins and
        # 'install' account that starts anaconda on login
        passwd = joinpaths(self.root, "etc", "passwd")
        with open(passwd, "a") as fobj:
            fobj.write("sshd:x:74:74:Privilege-separated "
                       "SSH:/var/empty/sshd:/sbin/nologin\n")
            fobj.write("install:x:0:0:root:/root:/sbin/loader\n")

        shadow = joinpaths(self.root, "etc", "shadow")
        with open(shadow, "w") as fobj:
            fobj.write("root::14438:0:99999:7:::\n")
            fobj.write("install::14438:0:99999:7:::\n")

        # change permissions
        chmod_(shadow, 400)

    def generate_ssh_keys(self):
        logger.info("generating SSH1 RSA host key")
        rsa1 = joinpaths(self.root, "etc/ssh/ssh_host_key")
        cmd = [self.lcmds.SSHKEYGEN, "-q", "-t", "rsa1", "-f", rsa1,
               "-C", "", "-N", ""]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        p.wait()

        logger.info("generating SSH2 RSA host key")
        rsa2 = joinpaths(self.root, "etc/ssh/ssh_host_rsa_key")
        cmd = [self.lcmds.SSHKEYGEN, "-q", "-t", "rsa", "-f", rsa2,
               "-C", "", "-N", ""]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        p.wait()

        logger.info("generating SSH2 DSA host key")
        dsa = joinpaths(self.root, "etc/ssh/ssh_host_dsa_key")
        cmd = [self.lcmds.SSHKEYGEN, "-q", "-t", "dsa", "-f", dsa,
               "-C", "", "-N", ""]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        p.wait()

        # change key file permissions
        for key in [rsa1, rsa2, dsa]:
            chmod_(key, 0600)
            chmod_(key + ".pub", 0644)


    def get_anaconda_portions(self):
        src = joinpaths(self.root, "usr", self.libdir, "anaconda", "loader")
        dst = joinpaths(self.root, "sbin")
        shutil.copy2(src, dst)

        src = joinpaths(self.root, "usr/share/anaconda", "loader.tr")
        dst = joinpaths(self.root, "etc")
        shutil.move(src, dst)

        src = joinpaths(self.root, "usr/libexec/anaconda", "auditd")
        dst = joinpaths(self.root, "sbin")
        shutil.copy2(src, dst)

    def compress(self, outfile, type="xz", speed="9"):
        chdir = lambda: os.chdir(self.root)
        start = time.time()

        find = subprocess.Popen([self.lcmds.FIND, "."], stdout=subprocess.PIPE,
                                preexec_fn=chdir)

        cpio = subprocess.Popen([self.lcmds.CPIO,
                                 "--quiet", "-H", "newc", "-o"],
                                stdin=find.stdout, stdout=subprocess.PIPE,
                                preexec_fn=chdir)

        compressed = subprocess.Popen([type, "-%s" % speed], stdin=cpio.stdout,
                                      stdout=open(outfile, "wb"))

        logger.debug("compressing")
        rc = compressed.wait()

        elapsed = time.time() - start

        return True, elapsed

    def install_kernel_modules(self, keepmodules):
        self.move_modules()
        for kernel in os.listdir(joinpaths(self.root, "modules")):
            self.cleanup_kernel_modules(keepmodules, kernel)
            self.compress_modules(kernel)
            self.run_depmod(kernel)
