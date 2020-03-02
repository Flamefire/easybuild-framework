#!/usr/bin/env python
##
# Copyright 2013-2020 Ghent University
#
# This file is part of EasyBuild,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://www.vscentrum.be),
# Flemish Research Foundation (FWO) (http://www.fwo.be/en)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# https://github.com/easybuilders/easybuild
#
# EasyBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EasyBuild.  If not, see <http://www.gnu.org/licenses/>.
##

"""
Bootstrap script for EasyBuild

Installs distribute with included (patched) distribute_setup.py script to obtain easy_install,
and then performs a staged install of EasyBuild:
 * stage 0: install setuptools (which provides easy_install), unless already available
 * stage 1: install EasyBuild with easy_install to a temporary directory
 * stage 2: install EasyBuild with EasyBuild from stage 1 to specified install directory

Authors: Kenneth Hoste (UGent), Stijn Deweirdt (UGent), Ward Poelmans (UGent)
License: GPLv2

inspired by https://bitbucket.org/pdubroy/pip/raw/tip/getpip.py
(via http://dubroy.com/blog/so-you-want-to-install-a-python-package/)
"""

import codecs
import copy
import glob
import os
import re
import shutil
import site
import sys
import tempfile
import traceback
from distutils.version import LooseVersion
from hashlib import md5
from platform import python_version

IS_PY3 = sys.version_info[0] == 3

if not IS_PY3:
    import urllib2 as std_urllib
else:
    import urllib.request as std_urllib


(??)EB_BOOTSTRAP_VERSION = '20200914.01'

# argparse preferrred, optparse deprecated >=2.7
HAVE_ARGPARSE = False
try:
    import argparse
    HAVE_ARGPARSE = True
except ImportError:
    import optparse

PYPI_SOURCE_URL = 'https://pypi.python.org/packages/source'

VSC_BASE = 'vsc-base'
VSC_INSTALL = 'vsc-install'
# Python 3 is not supported by the vsc-* packages
EASYBUILD_PACKAGES = (([] if IS_PY3 else [VSC_INSTALL, VSC_BASE]) +
                      ['easybuild-framework', 'easybuild-easyblocks', 'easybuild-easyconfigs'])

STAGE1_SUBDIR = 'eb_stage1'

# set print_debug to True for detailed progress info
print_debug = os.environ.pop('EASYBUILD_BOOTSTRAP_DEBUG', False)

# install with --force in stage2?
forced_install = os.environ.pop('EASYBUILD_BOOTSTRAP_FORCED', False)

# don't add user site directory to sys.path (equivalent to python -s), see https://www.python.org/dev/peps/pep-0370/
os.environ['PYTHONNOUSERSITE'] = '1'
site.ENABLE_USER_SITE = False

# clean PYTHONPATH to avoid finding readily installed stuff
os.environ['PYTHONPATH'] = ''

EASYBUILD_BOOTSTRAP_SOURCEPATH = os.environ.pop('EASYBUILD_BOOTSTRAP_SOURCEPATH', None)
EASYBUILD_BOOTSTRAP_SKIP_STAGE0 = os.environ.pop('EASYBUILD_BOOTSTRAP_SKIP_STAGE0', False)
EASYBUILD_BOOTSTRAP_FORCE_VERSION = os.environ.pop('EASYBUILD_BOOTSTRAP_FORCE_VERSION', None)

# keep track of original environment (after clearing PYTHONPATH)
orig_os_environ = copy.deepcopy(os.environ)

# If the modules tool is specified, use it
easybuild_modules_tool = os.environ.get('EASYBUILD_MODULES_TOOL', None)
easybuild_module_syntax = os.environ.get('EASYBUILD_MODULE_SYNTAX', None)

# If modules subdir specifications are defined, use them
easybuild_installpath_modules = os.environ.get('EASYBUILD_INSTALLPATH_MODULES', None)
easybuild_subdir_modules = os.environ.get('EASYBUILD_SUBDIR_MODULES', 'modules')
easybuild_suffix_modules_path = os.environ.get('EASYBUILD_SUFFIX_MODULES_PATH', 'all')


#
# Utility functions
#
def debug(msg):
    """Print debug message."""

    if print_debug:
        print("[[DEBUG]] " + msg)


def info(msg):
    """Print info message."""

    print("[[INFO]] " + msg)


def error(msg, exit=True):
    """Print error message and exit."""

    print("[[ERROR]] " + msg)
    sys.exit(1)


def mock_stdout_stderr():
    """Mock stdout/stderr channels"""
    try:
        from cStringIO import StringIO
    except ImportError:
        from io import StringIO
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    sys.stdout.flush()
    sys.stdout = StringIO()
    sys.stderr.flush()
    sys.stderr = StringIO()

    return orig_stdout, orig_stderr


def restore_stdout_stderr(orig_stdout, orig_stderr):
    """Restore stdout/stderr channels after mocking"""
    # collect output
    sys.stdout.flush()
    stdout = sys.stdout.getvalue()
    sys.stderr.flush()
    stderr = sys.stderr.getvalue()

    # restore original stdout/stderr
    sys.stdout = orig_stdout
    sys.stderr = orig_stderr

    return stdout, stderr


def det_lib_path(libdir):
    """Determine relative path of Python library dir."""
    if libdir is None:
        libdir = 'lib'
    pyver = '.'.join([str(x) for x in sys.version_info[:2]])
    return os.path.join(libdir, 'python%s' % pyver, 'site-packages')


def det_modules_path(install_path):
    """Determine modules path."""
    if easybuild_installpath_modules is not None:
        modules_path = os.path.join(easybuild_installpath_modules, easybuild_suffix_modules_path)
    else:
        modules_path = os.path.join(install_path, easybuild_subdir_modules, easybuild_suffix_modules_path)

    return modules_path


def find_egg_dir_for(path, pkg):
    """Find full path of egg dir for given package."""

    res = None

    for libdir in ['lib', 'lib64']:
        full_libpath = os.path.join(path, det_lib_path(libdir))
        eggdir_regex = re.compile('%s-[0-9a-z.]+.dist-info' % pkg.replace('-', '_'))
        subdirs = (os.path.exists(full_libpath) and sorted(os.listdir(full_libpath))) or []
        for subdir in subdirs:
            if eggdir_regex.match(subdir):
                eggdir = os.path.join(full_libpath, subdir)
                if res is None:
                    debug("Found egg dir for %s at %s" % (pkg, eggdir))
                    res = eggdir
                else:
                    debug("Found another egg dir for %s at %s (ignoring it)" % (pkg, eggdir))

    # no egg dir found
    if res is None:
        debug("Failed to determine egg dir path for %s in %s (subdirs: %s)" % (pkg, path, subdirs))

    return res


def prep(path):
    """Prepare for installing a Python package in the specified path."""

    debug("Preparing for path %s" % path)

    # restore original environment first
    os.environ = copy.deepcopy(orig_os_environ)
    debug("os.environ['PYTHONPATH'] after reset: %s" % os.environ['PYTHONPATH'])

    # update PATH
    os.environ['PATH'] = os.pathsep.join([os.path.join(path, 'bin')] +
                                         [x for x in os.environ.get('PATH', '').split(os.pathsep) if len(x) > 0])
    debug("$PATH: %s" % os.environ['PATH'])

    # update actual Python search path
    sys.path.insert(0, path)

    # make sure directory exists (this is required by setuptools)
    # usually it's 'lib', but can be 'lib64' as well
    for libdir in ['lib', 'lib64']:
        full_libpath = os.path.join(path, det_lib_path(libdir))
        if not os.path.exists(full_libpath):
            os.makedirs(full_libpath)
        # PYTHONPATH needs to be set as well, otherwise setuptools will fail
        pythonpaths = [x for x in os.environ.get('PYTHONPATH', '').split(os.pathsep) if len(x) > 0]
        os.environ['PYTHONPATH'] = os.pathsep.join([full_libpath] + pythonpaths)

    debug("$PYTHONPATH: %s" % os.environ['PYTHONPATH'])

    os.environ['EASYBUILD_MODULES_TOOL'] = easybuild_modules_tool
    debug("$EASYBUILD_MODULES_TOOL set to %s" % os.environ['EASYBUILD_MODULES_TOOL'])

    if easybuild_module_syntax:
        # if module syntax is specified, use it
        os.environ['EASYBUILD_MODULE_SYNTAX'] = easybuild_module_syntax
        debug("Using specified module syntax: %s" % os.environ['EASYBUILD_MODULE_SYNTAX'])
    elif easybuild_modules_tool != 'Lmod':
        # Lua is the default module syntax, but that requires Lmod
        # if Lmod is not being used, use Tcl module syntax
        os.environ['EASYBUILD_MODULE_SYNTAX'] = 'Tcl'
        debug("$EASYBUILD_MODULE_SYNTAX set to %s" % os.environ['EASYBUILD_MODULE_SYNTAX'])


def check_module_command(tmpdir):
    """Check which module command is available, and prepare for using it."""
    global easybuild_modules_tool

    if easybuild_modules_tool is not None:
        info("Using modules tool specified by $EASYBUILD_MODULES_TOOL: %s" % easybuild_modules_tool)
        return easybuild_modules_tool

    def check_cmd_help(modcmd):
        """Check 'help' output for specified command."""
        modcmd_re = re.compile(r'module\s.*command')
        cmd = "%s python help" % modcmd
        os.system("%s > %s 2>&1" % (cmd, out))
        txt = open(out, 'r').read()
        debug("Output from %s: %s" % (cmd, txt))
        return modcmd_re.search(txt)

    def is_modulecmd_tcl_modulestcl():
        """Determine if modulecmd.tcl is EnvironmentModulesTcl."""
        modcmd_re = re.compile('Modules Release Tcl')
        cmd = "modulecmd.tcl python --version"
        os.system("%s > %s 2>&1" % (cmd, out))
        txt = open(out, 'r').read()
        debug("Output from %s: %s" % (cmd, txt))
        return modcmd_re.search(txt)

    # order matters, which is why we don't use a dict
    known_module_commands = [
        ('lmod', 'Lmod'),
        ('modulecmd.tcl', 'EnvironmentModules'),
        ('modulecmd', 'EnvironmentModulesC'),
    ]
    out = os.path.join(tmpdir, 'module_command.out')
    modtool = None
    for modcmd, modtool in known_module_commands:
        if check_cmd_help(modcmd):
            # distinguish between EnvironmentModulesTcl and EnvironmentModules
            if modcmd == 'modulecmd.tcl' and is_modulecmd_tcl_modulestcl():
                modtool = 'EnvironmentModulesTcl'
            easybuild_modules_tool = modtool
            info("Found module command '%s' (%s), so using it." % (modcmd, modtool))
            break
        elif modcmd == 'lmod':
            # check value of $LMOD_CMD as fallback
            modcmd = os.environ.get('LMOD_CMD')
            if modcmd and check_cmd_help(modcmd):
                easybuild_modules_tool = modtool
                info("Found module command '%s' via $LMOD_CMD (%s), so using it." % (modcmd, modtool))
                break
        elif modtool == 'EnvironmentModules':
            # check value of $MODULESHOME as fallback
            moduleshome = os.environ.get('MODULESHOME', 'MODULESHOME_NOT_DEFINED')
            modcmd = os.path.join(moduleshome, 'libexec', 'modulecmd.tcl')
            if os.path.exists(modcmd) and check_cmd_help(modcmd):
                easybuild_modules_tool = modtool
                info("Found module command '%s' via $MODULESHOME (%s), so using it." % (modcmd, modtool))
                break

    if easybuild_modules_tool is None:
        mod_cmds = [m for (m, _) in known_module_commands]
        msg = [
            "Could not find any module command, make sure one available in your $PATH.",
            "Known module commands are checked in order, and include: %s" % ', '.join(mod_cmds),
            "Check the output of 'type module' to determine the location of the module command you are using.",
        ]
        error('\n'.join(msg))

    return modtool


def check_setuptools():
    """Check whether a suitable setuptools installation is already available."""

    debug("Checking whether suitable setuptools installation is available...")
    res = None

    _, outfile = tempfile.mkstemp()

    # note: we need to be very careful here, because switching to a different setuptools installation (e.g. in stage0)
    #       after the setuptools module was imported is very tricky...
    #       So, we'll check things by running commands through os.system rather than importing setuptools directly.
    cmd_tmpl = "%s -c '%%s' > %s 2>&1" % (sys.executable, outfile)

    os.system(cmd_tmpl % "from setuptools.command import easy_install; print(easy_install.__file__)")
    out = open(outfile).read().strip()
    if 'setuptools/command/easy_install' not in out:
        debug("Module 'setuptools.command.easy_install not found")
        res = False
    else:
        debug("Location of setuptools' easy_install module: %s" % out)

        # check setuptools version
        try:
            os.system(cmd_tmpl % "import setuptools; print(setuptools.__version__)")
            setuptools_ver = LooseVersion(open(outfile).read().strip())
            debug("Found setuptools version %s" % setuptools_ver)

            min_setuptools_ver = '0.6c11'
            if setuptools_ver < LooseVersion(min_setuptools_ver):
                debug("Minimal setuptools version %s not satisfied, found '%s'" % (min_setuptools_ver, setuptools_ver))
                res = False
        except Exception as err:
            debug("Failed to check setuptools version: %s" % err)
            res = False

        if res is None:
            os.system(cmd_tmpl % "import setuptools; print(setuptools.__file__)")
            setuptools_loc = open(outfile).read().strip()
            res = os.path.dirname(os.path.dirname(setuptools_loc))
            debug("Location of setuptools installation: %s" % res)

    try:
        os.remove(outfile)
    except Exception:
        pass

    return res

def run_command(cmd, quiet=False):
    """Run the given command and return its output (stdout and stderr)"""
    fd, out_file = tempfile.mkstemp()
    os.close(fd)
    os.system("%s > %s 2>&1" % (cmd, out_file))
    with open(out_file, "r") as f:
        txt = f.read().strip()
    os.remove(out_file)
    if not quiet:
        debug("Output from %s:\n%s" % (cmd, txt))
    return txt


def run_pip_install(args):
    run_command("pip install " + " ".join(['"%s"' % arg for arg in args]))


def stage1(tmpdir, sourcepath, distribute_egg_dir, forcedversion):
    """STAGE 1: temporary install EasyBuild using pip."""

    print('\n')
    info("+++ STAGE 1: installing EasyBuild in temporary dir with pip...\n")

    # determine locations of source tarballs, if sources path is specified
    source_tarballs = {}
    if sourcepath is not None:
        info("Fetching sources from %s..." % sourcepath)
        for pkg in EASYBUILD_PACKAGES:
            pkg_tarball_glob = os.path.join(sourcepath, '%s*.tar.gz' % pkg)
            pkg_tarball_paths = glob.glob(pkg_tarball_glob)
            if len(pkg_tarball_paths) > 1:
                error("Multiple tarballs found for %s: %s" % (pkg, pkg_tarball_paths))
            elif len(pkg_tarball_paths) == 0:
                if pkg not in [VSC_BASE, VSC_INSTALL]:
                    # vsc-base package is not strictly required
                    # it's only a dependency since EasyBuild v2.0;
                    # with EasyBuild v2.0, it will be pulled in from PyPI when installing easybuild-framework;
                    # vsc-install is an optional dependency, only required to run unit tests
                    error("Missing source tarball: %s" % pkg_tarball_glob)
            else:
                info("Found %s for %s package" % (pkg_tarball_paths[0], pkg))
                source_tarballs.update({pkg: pkg_tarball_paths[0]})

    if print_debug:
        debug("$ pip install --help")
        #run_pip_install(['--help'])

    # prepare install dir
    targetdir_stage1 = os.path.join(tmpdir, STAGE1_SUBDIR)
    prep(targetdir_stage1)  # set PATH, Python search path

    run_pip_install(['--prefix=%s' % targetdir_stage1, '--ignore-installed', 'pip'])
    run_pip_install(['--prefix=%s' % targetdir_stage1, '--ignore-installed', 'setuptools'])
    run_pip_install(['--prefix=%s' % targetdir_stage1, '--ignore-installed', 'wheel'])
    
    # install latest EasyBuild with pip from PyPi
    cmd = [
        '--upgrade',  # make sure the latest version is pulled from PyPi
        '--prefix=%s' % targetdir_stage1,
    ]

    post_vsc_base = []
    if source_tarballs:
        # install provided source tarballs (order matters)
        cmd.extend([source_tarballs[pkg] for pkg in EASYBUILD_PACKAGES if pkg in source_tarballs])
        # add vsc-base again at the end, to avoid that the one available on the system is used instead
        if VSC_BASE in source_tarballs:
            cmd.append(source_tarballs[VSC_BASE])
    else:
        # install meta-package easybuild from PyPI
        if forcedversion:
            cmd.append('easybuild==%s' % forcedversion)
        elif IS_PY3:
            cmd.append('easybuild>=4.0')  # Python 3 support added in EasyBuild 4
        else:
            cmd.append('easybuild')

        if not IS_PY3:
            # install vsc-base again at the end, to avoid that the one available on the system is used instead
            post_vsc_base = cmd[:]
            post_vsc_base[-1] = VSC_BASE + '<2.9.0'

    if not print_debug:
        cmd.insert(0, '--quiet')

    # There is no support for Python3 in the older vsc-* packages and EasyBuild 4 includes working versions of vsc-*
    if not IS_PY3:
        # install vsc-install version prior to 0.11.4, where mock was introduced as a dependency
        # workaround for problem reported in https://github.com/easybuilders/easybuild-framework/issues/2712
        # also stick to vsc-base < 2.9.0 to avoid requiring 'future' Python package as dependency
        for pkg in [VSC_INSTALL + '<0.11.4', VSC_BASE + '<2.9.0']:
            precmd = cmd[:-1] + [pkg]
            info("running pre-install command 'pip install %s'" % (' '.join(precmd)))
            run_pip_install(precmd)

    info("installing EasyBuild with 'pip install %s'\n" % (' '.join(cmd)))
    syntax_error_note = '\n'.join([
        "Note: a 'SyntaxError' may be reported for the easybuild/tools/py2vs3/py%s.py module." % ('3', '2')[IS_PY3],
        "You can safely ignore this message, it will not affect the functionality of the EasyBuild installation.",
        '',
    ])
    info(syntax_error_note)
    run_pip_install(cmd)

    if post_vsc_base:
        info("running post install command 'pip install %s'" % (' '.join(post_vsc_base)))
        run_pip_install(post_vsc_base)

        pkg_egg_dir = find_egg_dir_for(targetdir_stage1, VSC_BASE)
        if pkg_egg_dir is None:
            # if vsc-base available on system is the same version as the one being installed,
            # the .egg directory may not get installed...
            # in that case, try to have it *copied* by also including --always-copy;
            # using --always-copy should be used as a last resort, since it can result in all kinds of problems
            info(".egg dir for vsc-base not found, trying again with --always-copy...")
            post_vsc_base.insert(0, '--always-copy')
            info("running post install command 'pip install %s'" % (' '.join(post_vsc_base)))
            run_pip_install(post_vsc_base)
        vsc_dir = os.path.join(os.path.dirname(pkg_egg_dir), 'vsc')
        if not os.path.isdir(vsc_dir):
            raise RuntimeError("vsc not found")
        with open(os.path.join(vsc_dir, '__init__.py'), 'w') as f:
            f.write('# Namespace package')

    # clear the Python search path, we only want the individual eggs dirs to be in the PYTHONPATH (see below)
    # this is needed to avoid easy-install.pth controlling what Python packages are actually used
    #if distribute_egg_dir is not None:
    #    os.environ['PYTHONPATH'] = distribute_egg_dir
    #else:
    #    del os.environ['PYTHONPATH']

    # template string to inject in template easyconfig
    templates = {}

    for pkg in EASYBUILD_PACKAGES:
        templates.update({pkg: ''})

        pkg_egg_dir = find_egg_dir_for(targetdir_stage1, pkg)
        if pkg_egg_dir is None:
            if pkg in [VSC_BASE, VSC_INSTALL]:
                # vsc-base is optional in older EasyBuild versions
                continue

        # prepend EasyBuild egg dirs to Python search path, so we know which EasyBuild we're using
        sys.path.insert(0, os.path.dirname(pkg_egg_dir))
        pythonpaths = [x for x in os.environ.get('PYTHONPATH', '').split(os.pathsep) if len(x) > 0]
        os.environ['PYTHONPATH'] = os.pathsep.join([os.path.dirname(pkg_egg_dir)] + pythonpaths)
        debug("$PYTHONPATH: %s" % os.environ['PYTHONPATH'])

        if source_tarballs:
            if pkg in source_tarballs:
                templates.update({pkg: "'%s'," % os.path.basename(source_tarballs[pkg])})
        else:
            # determine per-package versions based on egg dirs, to use them in easyconfig template
            version_regex = re.compile('%s-([0-9a-z.-]*).dist-info' % pkg.replace('-', '_'))
            pkg_egg_dirname = os.path.basename(pkg_egg_dir)
            res = version_regex.search(pkg_egg_dirname)
            if res is not None:
                pkg_version = res.group(1)
                debug("Found version for easybuild-%s: %s" % (pkg, pkg_version))
                templates.update({pkg: "'%s-%s.tar.gz'," % (pkg, pkg_version)})
            else:
                tup = (pkg, pkg_egg_dirname, version_regex.pattern)
                error("Failed to determine version for easybuild-%s package from %s with %s" % tup)

    # figure out EasyBuild version via eb command line
    # note: EasyBuild uses some magic to determine the EasyBuild version based on the versions of the individual pkgs
    ver_regex = {'ver': '[0-9.]*[a-z0-9]*'}
    pattern = r"This is EasyBuild (?P<version>%(ver)s) \(framework: %(ver)s, easyblocks: %(ver)s\)" % ver_regex
    version_re = re.compile(pattern)
    version_out_file = os.path.join(tmpdir, 'eb_version.out')
    eb_version_cmd = 'from easybuild.tools.version import this_is_easybuild; print(this_is_easybuild())'
    cmd = "%s -c '%s' > %s 2>&1" % (sys.executable, eb_version_cmd, version_out_file)
    debug("Determining EasyBuild version using command '%s'" % cmd)
    os.system(cmd)
    txt = open(version_out_file, "r").read()
    res = version_re.search(txt)
    if res:
        eb_version = res.group(1)
        debug("installing EasyBuild v%s" % eb_version)
    else:
        error("Stage 1 failed, could not determine EasyBuild version (txt: %s)." % txt)

    templates.update({'version': eb_version})

    # clear PYTHONPATH before we go to stage2
    # PYTHONPATH doesn't need to (and shouldn't) include the stage1 egg dirs
    os.environ['PYTHONPATH'] = ''

    # make sure we're getting the expected EasyBuild packages
    import easybuild.framework
    import easybuild.easyblocks
    pkgs_to_check = [easybuild.framework, easybuild.easyblocks]
    # vsc is part of EasyBuild 4
    if LooseVersion(eb_version) < LooseVersion('4'):
        import vsc.utils.fancylogger
        pkgs_to_check.append(vsc.utils.fancylogger)

    for pkg in pkgs_to_check:
        if tmpdir not in pkg.__file__:
            error("Found another %s than expected: %s" % (pkg.__name__, pkg.__file__))
        else:
            debug("Found %s in expected path, good!" % pkg.__name__)

    debug("templates: %s" % templates)
    return templates


def stage2(tmpdir, templates, install_path, distribute_egg_dir, sourcepath):
    """STAGE 2: install EasyBuild to temporary dir with EasyBuild from stage 1."""

    print('\n')
    info("+++ STAGE 2: installing EasyBuild in %s with EasyBuild from stage 1...\n" % install_path)

    preinstallopts = ''

    eb_looseversion = LooseVersion(templates['version'])

    # setuptools is no longer required for EasyBuild v4.0 & newer, so skip the setuptools stuff in that case
    if eb_looseversion < LooseVersion('4.0') and distribute_egg_dir is not None:
        # inject path to distribute installed in stage 0 into $PYTHONPATH via preinstallopts
        # other approaches are not reliable, since EasyBuildMeta easyblock unsets $PYTHONPATH;
        # this is required for the easy_install from stage 0 to work
        preinstallopts += "export PYTHONPATH=%s:$PYTHONPATH && " % distribute_egg_dir

        # ensure that (latest) setuptools is installed as well alongside EasyBuild,
        # since it is a required runtime dependency for recent vsc-base and EasyBuild versions
        # this is necessary since we provide our own distribute installation during the bootstrap (cfr. stage0)
        preinstallopts += "%s -m easy_install -U --prefix %%(installdir)s setuptools && " % sys.executable

    # vsc-install is no longer required for EasyBuild v4.0, so skip pre-installed vsc-install in that case
    if eb_looseversion < LooseVersion('4.0'):
        # vsc-install is a runtime dependency for the EasyBuild unit test suite,
        # and is easily picked up from stage1 rather than being actually installed, so force it
        vsc_install = "'%s<0.11.4'" % VSC_INSTALL
        if sourcepath:
            vsc_install_tarball_paths = glob.glob(os.path.join(sourcepath, 'vsc-install*.tar.gz'))
            if len(vsc_install_tarball_paths) == 1:
                vsc_install = vsc_install_tarball_paths[0]
        preinstallopts += "%s -m easy_install -U --prefix %%(installdir)s %s && " % (sys.executable, vsc_install)

    templates.update({
        'preinstallopts': preinstallopts,
    })

    # determine PyPI URLs for individual packages
    pkg_urls = []
    for pkg in EASYBUILD_PACKAGES:

        # vsc-base and vsc-install are not dependencies anymore for EasyBuild v4.0,
        # so skip them here for recent EasyBuild versions
        if eb_looseversion >= LooseVersion('4.0') and pkg in [VSC_INSTALL, VSC_BASE]:
            continue

        # format of pkg entries in templates: "'<pkg_filename>',"
        pkg_filename = templates[pkg][1:-2]

        # the lines below implement a simplified version of the 'pypi_source_urls' and 'derive_alt_pypi_url' functions,
        # which we can't leverage here, partially because of transitional changes in PyPI (#md5= -> #sha256=)

        # determine download URL via PyPI's 'simple' API
        pkg_simple = None
        try:
            pkg_simple = std_urllib.urlopen('https://pypi.python.org/simple/%s' % pkg, timeout=10).read()
        except (std_urllib.URLError, std_urllib.HTTPError) as err:
            # failing to figure out the package download URl may be OK when source tarballs are provided
            if sourcepath:
                info("Ignoring failed attempt to determine '%s' download URL since source tarballs are provided" % pkg)
            else:
                raise err

        if pkg_simple:
            if IS_PY3:
                pkg_simple = pkg_simple.decode('utf-8')
            pkg_url_part_regex = re.compile('/(packages/[^#]+)/%s#' % pkg_filename)
            res = pkg_url_part_regex.search(pkg_simple)
            if res:
                pkg_url = 'https://pypi.python.org/' + res.group(1)
                pkg_urls.append(pkg_url)
            elif sourcepath:
                info("Ignoring failure to determine source URL for '%s' (source tarballs are provided)" % pkg_filename)
            else:
                error_msg = "Failed to determine PyPI package URL for %s using pattern '%s': %s\n"
                error(error_msg % (pkg, pkg_url_part_regex.pattern, pkg_simple))

    # vsc-base and vsc-install are no longer required for EasyBuild v4.0.0,
    # so only include them in 'sources' for older versions
    sources_tmpl = "%(easybuild-framework)s%(easybuild-easyblocks)s%(easybuild-easyconfigs)s"
    if eb_looseversion < LooseVersion('4.0'):
        sources_tmpl = "%(vsc-install)s%(vsc-base)s" + sources_tmpl
        templates['toolchain'] = EASYBUILD_EASYCONFIG_TOOLCHAIN_PRE4
    else:
        templates['toolchain'] = EASYBUILD_EASYCONFIG_TOOLCHAIN


    templates.update({
        'source_urls': '\n'.join(["'%s'," % x for x in pkg_urls]),
        'sources': sources_tmpl % templates,
        'pythonpath': distribute_egg_dir,
    })

    # create easyconfig file
    ebfile = os.path.join(tmpdir, 'EasyBuild-%s.eb' % templates['version'])
    handle = open(ebfile, 'w')
    ebfile_txt = EASYBUILD_EASYCONFIG_TEMPLATE % templates
    handle.write(ebfile_txt)
    handle.close()
    debug("Contents of generated easyconfig file:\n%s" % ebfile_txt)

    # set command line arguments for eb
    eb_args = ['eb', ebfile, '--allow-modules-tool-mismatch']
    if print_debug:
        eb_args.extend(['--debug', '--logtostdout'])
    if forced_install:
        info("Performing FORCED installation, as requested...")
        eb_args.append('--force')

    # make sure we don't leave any stuff behind in default path $HOME/.local/easybuild
    # and set build and install path explicitely
    if LooseVersion(templates['version']) < LooseVersion('1.3.0'):
        os.environ['EASYBUILD_PREFIX'] = tmpdir
        os.environ['EASYBUILD_BUILDPATH'] = tmpdir
        if install_path is not None:
            os.environ['EASYBUILD_INSTALLPATH'] = install_path
    else:
        # only for v1.3 and up
        eb_args.append('--prefix=%s' % tmpdir)
        eb_args.append('--buildpath=%s' % tmpdir)
        if install_path is not None:
            eb_args.append('--installpath=%s' % install_path)
        if sourcepath is not None:
            eb_args.append('--sourcepath=%s' % sourcepath)

        # make sure EasyBuild can find EasyBuild-*.eb easyconfig file when it needs to;
        # (for example when HierarchicalMNS is used as module naming scheme,
        #  see https://github.com/easybuilders/easybuild-framework/issues/2393)
        eb_args.append('--robot-paths=%s:' % tmpdir)

    # make sure parent modules path already exists (Lmod trips over a non-existing entry in $MODULEPATH)
    if install_path is not None:
        modules_path = det_modules_path(install_path)
        if not os.path.exists(modules_path):
            os.makedirs(modules_path)
        debug("Created path %s" % modules_path)

    debug("Running EasyBuild with arguments '%s'" % ' '.join(eb_args))
    sys.argv = eb_args

    # location to 'eb' command (from stage 1) may be expected to be included in $PATH
    # it usually is there after stage1, unless 'prep' is called again with another location
    # (only when stage 0 is not skipped)
    # cfr. https://github.com/easybuilders/easybuild-framework/issues/2279
    curr_path = [x for x in os.environ.get('PATH', '').split(os.pathsep) if len(x) > 0]
    os.environ['PATH'] = os.pathsep.join([os.path.join(tmpdir, STAGE1_SUBDIR, 'bin')] + curr_path)
    debug("$PATH: %s" % os.environ['PATH'])

    # install EasyBuild with EasyBuild
    from easybuild.main import main as easybuild_main
    easybuild_main()

    if print_debug:
        os.environ['EASYBUILD_DEBUG'] = '1'

    # make sure the EasyBuild module was actually installed
    # EasyBuild configuration options that are picked up from configuration files/environment may break the bootstrap,
    # for example by having $EASYBUILD_VERSION defined or via a configuration file specifies a value for 'stop'...
    from easybuild.tools.config import build_option, install_path, get_module_syntax
    from easybuild.framework.easyconfig.easyconfig import ActiveMNS
    eb_spec = {
        'name': 'EasyBuild',
        'hidden': False,
        'toolchain': templates['toolchain'],
        'version': templates['version'],
        'versionprefix': '',
        'versionsuffix': '',
        'moduleclass': 'tools',
    }

    mod_path = os.path.join(install_path('mod'), build_option('suffix_modules_path'))
    debug("EasyBuild module should have been installed to %s" % mod_path)

    eb_mod_name = ActiveMNS().det_full_module_name(eb_spec)
    debug("EasyBuild module name: %s" % eb_mod_name)

    eb_mod_path = os.path.join(mod_path, eb_mod_name)
    if get_module_syntax() == 'Lua':
        eb_mod_path += '.lua'

    if os.path.exists(eb_mod_path):
        info("EasyBuild module installed: %s" % eb_mod_path)
    else:
        error("EasyBuild module not found at %s, define $EASYBUILD_BOOTSTRAP_DEBUG to debug" % eb_mod_path)


def main():
    """Main script: bootstrap EasyBuild in stages."""

    self_txt = open(__file__).read()
    if IS_PY3:
        self_txt = self_txt.encode('utf-8')
    info("EasyBuild bootstrap script (version %s, MD5: %s)" % (EB_BOOTSTRAP_VERSION, md5(self_txt).hexdigest()))
    info("Found Python %s\n" % '; '.join(sys.version.split('\n')))

    # disallow running as root, since stage 2 will fail
    if os.getuid() == 0:
        error("Don't run the EasyBuild bootstrap script as root, "
              "since stage 2 (installing EasyBuild with EasyBuild) will fail.")

    # general option/argument parser
    if HAVE_ARGPARSE:
        bs_argparser = argparse.ArgumentParser()
        bs_argparser.add_argument("prefix", help="Installation prefix directory",
                                  type=str)
        bs_args = bs_argparser.parse_args()

        # prefix specification
        install_path = os.path.abspath(bs_args.prefix)
    else:
        bs_argparser = optparse.OptionParser(usage="usage: %prog [options] prefix")
        (bs_opts, bs_args) = bs_argparser.parse_args()

        # poor method, but should prefer argparse module for better pos arg support.
        if len(bs_args) < 1:
            error("Too few arguments\n" + bs_argparser.get_usage())
        elif len(bs_args) > 1:
            error("Too many arguments\n" + bs_argparser.get_usage())

        # prefix specification
        install_path = os.path.abspath(str(bs_args[0]))

    info("Installation prefix %s" % install_path)

    sourcepath = EASYBUILD_BOOTSTRAP_SOURCEPATH
    if sourcepath is not None:
        info("Fetching sources from %s..." % sourcepath)

    forcedversion = EASYBUILD_BOOTSTRAP_FORCE_VERSION
    if forcedversion:
        info("Forcing specified version %s..." % forcedversion)
        if IS_PY3 and LooseVersion(forcedversion) < LooseVersion('4'):
            error('Python 3 support is only available with EasyBuild 4.x but you are trying to install EasyBuild %s'
                  % forcedversion)

    # create temporary dir for temporary installations
    tmpdir = tempfile.mkdtemp()
    debug("Going to use %s as temporary directory" % tmpdir)
    os.chdir(tmpdir)

    # check whether a module command is available, we need that
    modtool = check_module_command(tmpdir)

    # clean sys.path, remove paths that may contain EasyBuild packages or stuff installed with easy_install
    orig_sys_path = sys.path[:]
    sys.path = []
    for path in orig_sys_path:
        include_path = True
        # exclude path if it's potentially an EasyBuild/VSC package, providing the 'easybuild'/'vsc' namespace, resp.
        if any([os.path.exists(os.path.join(path, pkg, '__init__.py')) for pkg in ['easyblocks', 'easybuild', 'vsc']]):
            include_path = False
        # exclude any .egg paths
        if path.endswith('.egg'):
            include_path = False
        # exclude any path that contains an easy-install.pth file
        if os.path.exists(os.path.join(path, 'easy-install.pth')):
            include_path = False

        if include_path:
            sys.path.append(path)
        else:
            debug("Excluding %s from sys.path" % path)

    debug("sys.path after cleaning: %s" % sys.path)

    # install EasyBuild in stages

    # STAGE 0: install distribute, which delivers easy_install
    distribute_egg_dir = None
    if EASYBUILD_BOOTSTRAP_SKIP_STAGE0:
        info("Skipping stage 0, using local distribute/setuptools providing easy_install")
    else:
        try:
            import pip
            del pip
            info("Suitable pip installation already found, skipping stage 0...")
        except ImportError:
            info("No suitable pip installation found, proceeding with stage 0...")
            raise RuntimeError("Implement get-pip")
    prep(tmpdir)
    run_pip_install(['--prefix=%s' % tmpdir, '--ignore-installed', 'pip'])
    run_pip_install(['--prefix=%s' % tmpdir, '--ignore-installed', 'setuptools'])
    run_pip_install(['--prefix=%s' % tmpdir, '--ignore-installed', 'wheel'])
    stage0_lib = os.path.dirname(find_egg_dir_for(tmpdir, 'setuptools'))

    # STAGE 1: install EasyBuild using easy_install to tmp dir
    templates = stage1(tmpdir, sourcepath, distribute_egg_dir, forcedversion)

    # add location to easy_install provided through stage0 to $PATH
    # this must be done *after* stage1, since $PATH is reset during stage1
    prep(tmpdir)

    # STAGE 2: install EasyBuild using EasyBuild (to final target installation dir)
    stage2(tmpdir, templates, install_path, stage0_lib, sourcepath)

    # clean up the mess
    debug("Cleaning up %s..." % tmpdir)
    shutil.rmtree(tmpdir)

    print('')
    info('Bootstrapping EasyBuild completed!\n')

    if install_path is not None:
        info('EasyBuild v%s was installed to %s, so make sure your $MODULEPATH includes %s' %
             (templates['version'], install_path, det_modules_path(install_path)))
    else:
        info('EasyBuild v%s was installed to configured install path, make sure your $MODULEPATH is set correctly.' %
             templates['version'])
        info('(default config => add "$HOME/.local/easybuild/modules/all" in $MODULEPATH)')

    print('')
    info("Run 'module load EasyBuild', and run 'eb --help' to get help on using EasyBuild.")
    info("Set $EASYBUILD_MODULES_TOOL to '%s' to use the same modules tool as was used now." % modtool)
    print('')
    info("By default, EasyBuild will install software to $HOME/.local/easybuild.")
    info("To install software with EasyBuild to %s, set $EASYBUILD_INSTALLPATH accordingly." % install_path)
    info("See http://easybuild.readthedocs.org/en/latest/Configuration.html for details on configuring EasyBuild.")


# template easyconfig file for EasyBuild
EASYBUILD_EASYCONFIG_TEMPLATE = """
easyblock = 'EB_EasyBuildMeta'

name = 'EasyBuild'
version = '%(version)s'

homepage = 'http://easybuilders.github.com/easybuild/'
description = \"\"\"EasyBuild is a software build and installation framework
written in Python that allows you to install software in a structured,
repeatable and robust way.\"\"\"

toolchain = %(toolchain)s

source_urls = [%(source_urls)s]
sources = [%(sources)s]

# EasyBuild is a (set of) Python packages, so it depends on Python
# usually, we want to use the system Python, so no actual Python dependency is listed
allow_system_deps = [('Python', SYS_PYTHON_VERSION)]

preinstallopts = "%(preinstallopts)s"

sanity_check_paths = {
    'files': ['bin/eb'],
    'dirs': ['lib'],
}

moduleclass = 'tools'
"""
# Toolchain to be used for EasyBuild < 4.0
EASYBUILD_EASYCONFIG_TOOLCHAIN_PRE4 = {'name': 'dummy', 'version': 'dummy'}
# Toolchain to be used for EasyBuild >= 4.0 (new default)
EASYBUILD_EASYCONFIG_TOOLCHAIN = {'name': 'system', 'version': ''}

# check Python version
loose_pyver = LooseVersion(python_version())
min_pyver2 = LooseVersion('2.6')
min_pyver3 = LooseVersion('3.5')
if loose_pyver < min_pyver2 or (loose_pyver >= LooseVersion('3') and loose_pyver < min_pyver3):
    sys.stderr.write("ERROR: Incompatible Python version: %s (should be Python 2 >= %s or Python 3 >= %s)\n"
                     % (python_version(), min_pyver2, min_pyver3))
    sys.exit(1)

# run main function as body of script
main()
