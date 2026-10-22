
"""Resolve EasyBuild's dynamically exposed easyblock aliases from the test sandbox."""

import astroid
import importlib
import os
import sys
from astroid.exceptions import AstroidBuildingError

sys.path.insert(0, os.path.abspath("test/framework/sandbox"))


def log(*s):
    """Write to tempt file in known location for debugging"""
    with open('/tmp/eb_test.log', 'a') as f:
        print(*s, file=f)


def register(_linter):
    """Register the EasyBuild alias resolver."""

    PREFIX = "easybuild.easyblocks."

    def resolve_alias(module_name):
        if not module_name.startswith(PREFIX):
            raise AstroidBuildingError("Not an EasyBuild easyblock alias")
        suffix = module_name[len(PREFIX):]
        # Only handle a top-level alias, not nested modules.
        if not suffix or "." in suffix:
            raise AstroidBuildingError("Not a top-level easyblock alias")
        target = f"{PREFIX}{suffix[0]}.{suffix}"
        try:
            spec = importlib.util.find_spec(target)
        except (ImportError, ModuleNotFoundError, ValueError):
            pass
        try:
            if spec.origin is not None:
                return astroid.MANAGER.ast_from_file(spec.origin, modname=module_name)
        except (AttributeError, astroid.AstroidBuildingError):
            pass
        raise astroid.AstroidBuildingError(f"Could not resolve EasyBuild alias: {module_name}")

    astroid.MANAGER.register_failed_import_hook(resolve_alias)
