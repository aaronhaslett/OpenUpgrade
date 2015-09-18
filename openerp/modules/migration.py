# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

""" Modules migration handling. """

import imp
import logging
import os
from os.path import join as opj

import openerp
import openerp.release as release
import openerp.tools as tools
from openerp.tools.parse_version import parse_version


_logger = logging.getLogger(__name__)

class MigrationManager(object):
    """
        This class manage the migration of modules
        Migrations files must be python files containing a "migrate(cr, installed_version)" function.
        Theses files must respect a directory tree structure: A 'migrations' folder which containt a
        folder by version. Version can be 'module' version or 'server.module' version (in this case,
        the files will only be processed by this version of the server). Python file names must start
        by 'pre' or 'post' and will be executed, respectively, before and after the module initialisation
        Example:

            <moduledir>
            `-- migrations
                |-- 1.0
                |   |-- pre-update_table_x.py
                |   |-- pre-update_table_y.py
                |   |-- post-clean-data.py
                |   `-- README.txt              # not processed
                |-- 5.0.1.1                     # files in this folder will be executed only on a 5.0 server
                |   |-- pre-delete_table_z.py
                |   `-- post-clean-data.py
                `-- foo.py                      # not processed

        This similar structure is generated by the maintenance module with the migrations files get by
        the maintenance contract

    """
    def __init__(self, cr, graph):
        self.cr = cr
        self.graph = graph
        self.migrations = {}
        self._get_files()

    def _get_files(self):

        """
        import addons.base.maintenance.utils as maintenance_utils
        maintenance_utils.update_migrations_files(self.cr)
        #"""

        for pkg in self.graph:
            self.migrations[pkg.name] = {}
            if not (hasattr(pkg, 'update') or pkg.state == 'to upgrade'):
                continue

            get_module_filetree = openerp.modules.module.get_module_filetree
            self.migrations[pkg.name]['module'] = get_module_filetree(pkg.name, 'migrations') or {}
            self.migrations[pkg.name]['maintenance'] = get_module_filetree('base', 'maintenance/migrations/' + pkg.name) or {}

    def migrate_module(self, pkg, stage):
        assert stage in ('pre', 'post')
        stageformat = {
            'pre': '[>%s]',
            'post': '[%s>]',
        }
        # In openupgrade, remove 'or pkg.installed_version is None'
        # We want to always pass in pre and post migration files and use a new
        # argument in the migrate decorator (explained in the docstring)
        # to decide if we want to do something if a new module is installed
        # during the migration.
        if not (hasattr(pkg, 'update') or pkg.state == 'to upgrade'):
            return

        def convert_version(version):
            if version.count('.') >= 2:
                return version  # the version number already containt the server version
            return "%s.%s" % (release.major_version, version)

        def _get_migration_versions(pkg):
            def __get_dir(tree):
                return [d for d in tree if tree[d] is not None]

            versions = list(set(
                __get_dir(self.migrations[pkg.name]['module']) +
                __get_dir(self.migrations[pkg.name]['maintenance'])
            ))
            versions.sort(key=lambda k: parse_version(convert_version(k)))
            return versions

        def _get_migration_files(pkg, version, stage):
            """ return a list of tuple (module, file)
            """
            m = self.migrations[pkg.name]
            lst = []

            mapping = {
                'module': opj(pkg.name, 'migrations'),
                'maintenance': opj('base', 'maintenance', 'migrations', pkg.name),
            }

            for x in mapping.keys():
                if version in m[x]:
                    for f in m[x][version]:
                        if m[x][version][f] is not None:
                            continue
                        if not f.startswith(stage + '-'):
                            continue
                        lst.append(opj(mapping[x], version, f))
            lst.sort()
            return lst

        def mergedict(a, b):
            a = a.copy()
            a.update(b)
            return a

        parsed_installed_version = parse_version(pkg.installed_version or '')
        current_version = parse_version(convert_version(pkg.data['version']))

        versions = _get_migration_versions(pkg)

        for version in versions:
            if parsed_installed_version < parse_version(convert_version(version)) <= current_version:

                strfmt = {'addon': pkg.name,
                          'stage': stage,
                          'version': stageformat[stage] % version,
                          }

                for pyfile in _get_migration_files(pkg, version, stage):
                    name, ext = os.path.splitext(os.path.basename(pyfile))
                    if ext.lower() != '.py':
                        continue
                    # OpenUpgrade edit start:
                    # Removed a copy of migration script to temp directory
                    # Replaced call to load_source with load_module so frame isn't lost and breakpoints can be set
                    mod = fp = None
                    try:
                        fp, pathname = tools.file_open(pyfile, pathinfo=True)
                        try:
                            mod = imp.load_module(name, fp, pathname, ('.py', 'r', imp.PY_SOURCE))
                            _logger.info('module %(addon)s: Running migration %(version)s %(name)s',
                                         mergedict({'name': mod.__name__}, strfmt))
                        except ImportError:
                            _logger.exception('module %(addon)s: Unable to load %(stage)s-migration file %(file)s',
                                              mergedict({'file': pyfile}, strfmt))
                            raise

                        _logger.info('module %(addon)s: Running migration %(version)s %(name)s',
                                     mergedict({'name': mod.__name__}, strfmt))

                        if hasattr(mod, 'migrate'):
                            mod.migrate(self.cr, pkg.installed_version)
                        else:
                            _logger.error('module %(addon)s: Each %(stage)s-migration file must have a "migrate(cr, installed_version)" function',
                                          strfmt)
                    finally:
                        if fp:
                            fp.close()
                        if mod:
                            del mod
                    # OpenUpgrade edit end
