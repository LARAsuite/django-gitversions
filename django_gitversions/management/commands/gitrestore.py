from __future__ import unicode_literals

import glob
import gzip
import os
import warnings
import zipfile
from itertools import product
from django.apps import apps
from django.conf import settings
from django.core import serializers
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import (
    DEFAULT_DB_ALIAS, DatabaseError, IntegrityError, connections, router,
    transaction,
)
from django.core.serializers.base import DeserializationError
from django.utils import lru_cache
from django.utils._os import upath
from django.utils.deprecation import RemovedInDjango19Warning
from django.utils.encoding import force_text
from django.utils.functional import cached_property
from django.db.models.signals import post_save
from django_gitversions.signals import gitversion
from django_gitversions.backends.git import GitBackend
from django_gitversions import versioner

try:
    import bz2
    has_bz2 = True
except ImportError:
    has_bz2 = False


def get_all_fixtures():

    fixtures = []

    for app_path in glob.glob("/srv/leonardo/sites/leonardo/backup/*"):
        for model_path in glob.glob(app_path + '/*'):
            for instance_path in glob.glob(model_path + '/*.json'):
                fixtures.append(instance_path)
    return fixtures


def save_all(objects, using, iterations=0, stdout=None):
    '''Tries recursively saves all objects'''

    if len(objects) == 0 or iterations >= 25:
        return iterations

    skiped = []

    for obj in objects:
        if router.allow_migrate_model(using, obj.object.__class__):
            try:
                obj.save(using=using)
            except (DatabaseError, IntegrityError) as e:
                e.args = ("Could not load %(app_label)s.%(object_name)s(pk=%(pk)s): %(error_msg)s" % {
                    'app_label': obj.object._meta.app_label,
                    'object_name': obj.object._meta.object_name,
                    'pk': obj.object.pk,
                    'error_msg': force_text(e)
                },)
                # raise
                skiped.append(obj)

    if len(skiped) > 0:
        return save_all(skiped, using, iterations + 1)

    return iterations


class Command(BaseCommand):
    help = 'Installs the named fixture(s) in the database.'

    def add_arguments(self, parser):
        parser.add_argument('--database', action='store', dest='database',
                            default=DEFAULT_DB_ALIAS, help='Nominates a specific database to load '
                            'fixtures into. Defaults to the "default" database.')
        parser.add_argument('--app', action='store', dest='app_label',
                            default=None, help='Only look for fixtures in the specified app.')
        parser.add_argument('--ignorenonexistent', '-i', action='store_true',
                            dest='ignore', default=False,
                            help='Ignores entries in the serialized data for fields that do not '
                            'currently exist on the model.')
        parser.add_argument('--url', default=False, dest='url',
                            help='Remote url for restore.')

    def handle(self, *fixture_labels, **options):

        self.ignore = options.get('ignore')
        self.using = options.get('database')
        self.url = options.get('url')
        self.app_label = options.get('app_label')
        self.hide_empty = options.get('hide_empty', False)
        self.verbosity = options.get('verbosity')

        if self.url:
            # inicialize local backup repository
            self.stdout.write('Clonning initial data from {} into {}'.format(self.url, versioner.path))
            GitBackend(url=self.url).repo

        total = get_all_fixtures()
        unloaded = total
        count_total = len(total)
        processed = []
        skiped = []
        really_skiped = []
        objects = []

        post_save.disconnect(gitversion)

        connection = connections[self.using]

        i = 0
        while len(unloaded) > 0:

            try:
                with transaction.atomic(using=self.using) and connection.constraint_checks_disabled():
                    unloaded, _processed, _skiped, _objects, missing_fks = self.loaddata(
                        unloaded)
                    processed += _processed
                    skiped += _skiped
                    objects += _objects
            except transaction.TransactionManagementError:
                # somethink went wrong process again
                unloaded += _processed
            i += 1

        with transaction.atomic(using=self.using) and connection.constraint_checks_disabled():
            iterations = save_all(missing_fks, self.using)
            iterations = save_all(objects, self.using)

        # try again load some skiped objects
        while len(skiped) > 0:
            connection = connections[self.using]

            with transaction.atomic(using=self.using) and connection.constraint_checks_disabled():
                skiped, _processed, _really_skiped, _objects, missing_fks = self.loaddata(
                    skiped)
                processed += _processed
                really_skiped += _really_skiped
                objects += _objects

            i += 1

        # also try save again
        with transaction.atomic(using=self.using) and connection.constraint_checks_disabled():
            iterations = save_all(objects, self.using, iterations)

        self.stdout.write('Loaded %s instances and %s was skipped from total %s in %s loaddata iterations and %s saving iterations.' % (
            len(processed), len(really_skiped), count_total, i, iterations))
        # Close the DB connection -- unless we're still in a transaction. This
        # is required as a workaround for an  edge case in MySQL: if the same
        # connection is used to create tables, load data, and query, the query
        # can return incorrect results. See Django #7572, MySQL #37735.
        if transaction.get_autocommit(self.using):
            connections[self.using].close()

        post_save.connect(gitversion)

    def loaddata(self, fixture_labels):
        connection = connections[self.using]

        # Keep a count of the installed objects and fixtures
        self.fixture_count = 0
        self.loaded_object_count = 0
        self.fixture_object_count = 0
        self.models = set()

        self.serialization_formats = serializers.get_public_serializer_formats()
        # Forcing binary mode may be revisited after dropping Python 2 support
        # (see #22399)
        self.compression_formats = {
            None: (open, 'rb'),
            'gz': (gzip.GzipFile, 'rb'),
            'zip': (SingleZipReader, 'r'),
        }
        if has_bz2:
            self.compression_formats['bz2'] = (bz2.BZ2File, 'r')

        skiped = []
        processed = []
        loaded_objects = []
        missing_model = []
        missing_fks = []

        with connection.constraint_checks_disabled():
            # for fixture_label in fixture_labels:
            #    self.load_label(fixture_label)
            objects_in_fixture = 0
            loaded_objects_in_fixture = 0
            for path in fixture_labels:
                if self.verbosity >= 2:
                    self.stdout.write("Installing %s fixture" %
                                      (humanize(path)))

                with open(path, 'r') as fixture_file:
                    self.fixture_count += 1
                    try:
                        data = fixture_file.read()
                        objects = serializers.deserialize('json', data,
                                                          using=self.using, ignorenonexistent=self.ignore)
                        # evaluate
                        objects = list(objects)

                    except DeserializationError as ex:
                        skiped.append(path)
                        fixture_labels.remove(path)
                        # little comic there
                        if 'Invalid model identifier' in str(ex):
                            missing_model.append(str(ex))
                        elif 'matching query does not exist' in str(ex):
                            missing_fks += objects
                        else:
                            self.stderr.write(
                                'DeserializationError(%s) raised during serialization %s fixture.' % (ex, path))
                    except Exception as e:
                        fixture_labels.remove(path)
                        self.stderr.write(
                            'Exception %s %s raised during loading %s fixture.' % (str(e), e.__class__.__name__, path))
                    else:
                        # everythink is ok
                        loaded_objects += objects
                        processed.append(path)
                        fixture_labels.remove(path)

            # raise Exception(unloaded)
            self.loaded_object_count += loaded_objects_in_fixture
            self.fixture_object_count += objects_in_fixture
        return fixture_labels, processed, skiped, loaded_objects, missing_fks

        # Since we disabled constraint checks, we must manually check for
        # any invalid keys that might have been added
        table_names = [model._meta.db_table for model in self.models]
        try:
            connection.check_constraints(table_names=table_names)
        except Exception as e:
            e.args = ("Problem installing fixtures: %s" % e,)
            raise

        # If we found even one object in a fixture, we need to reset the
        # database sequences.
        if self.loaded_object_count > 0:
            sequence_sql = connection.ops.sequence_reset_sql(
                no_style(), self.models)
            if sequence_sql:
                if self.verbosity >= 2:
                    self.stdout.write("Resetting sequences\n")
                with connection.cursor() as cursor:
                    for line in sequence_sql:
                        cursor.execute(line)

        if self.verbosity >= 1:
            if self.fixture_count == 0 and self.hide_empty:
                pass
            elif self.fixture_object_count == self.loaded_object_count:
                self.stdout.write("Installed %d object(s) from %d fixture(s)" %
                                  (self.loaded_object_count, self.fixture_count))
            else:
                self.stdout.write("Installed %d object(s) (of %d) from %d fixture(s)" %
                                  (self.loaded_object_count, self.fixture_object_count, self.fixture_count))

    def load_label(self, fixture_label):
        """
        Loads fixtures files for a given label.
        """
        for fixture_file, fixture_dir, fixture_name in self.find_fixtures(fixture_label):
            _, ser_fmt, cmp_fmt = self.parse_name(
                os.path.basename(fixture_file))
            open_method, mode = self.compression_formats[cmp_fmt]
            fixture = open_method(fixture_file, mode)
            try:
                self.fixture_count += 1
                objects_in_fixture = 0
                loaded_objects_in_fixture = 0
                if self.verbosity >= 2:
                    self.stdout.write("Installing %s fixture '%s' from %s." %
                                      (ser_fmt, fixture_name, humanize(fixture_dir)))

                objects = serializers.deserialize(ser_fmt, fixture,
                                                  using=self.using, ignorenonexistent=self.ignore)

                for obj in objects:
                    objects_in_fixture += 1
                    if router.allow_migrate_model(self.using, obj.object.__class__):
                        loaded_objects_in_fixture += 1
                        self.models.add(obj.object.__class__)
                        try:
                            obj.save(using=self.using)
                        except (DatabaseError, IntegrityError) as e:
                            e.args = ("Could not load %(app_label)s.%(object_name)s(pk=%(pk)s): %(error_msg)s" % {
                                'app_label': obj.object._meta.app_label,
                                'object_name': obj.object._meta.object_name,
                                'pk': obj.object.pk,
                                'error_msg': force_text(e)
                            },)
                            raise

                self.loaded_object_count += loaded_objects_in_fixture
                self.fixture_object_count += objects_in_fixture
            except Exception as e:
                if not isinstance(e, CommandError):
                    e.args = ("Problem installing fixture '%s': %s" % (
                        fixture_file, e),)
                raise
            finally:
                fixture.close()

            # Warn if the fixture we loaded contains 0 objects.
            if objects_in_fixture == 0:
                warnings.warn(
                    "No fixture data found for '%s'. (File format may be "
                    "invalid.)" % fixture_name,
                    RuntimeWarning
                )

    @lru_cache.lru_cache(maxsize=None)
    def find_fixtures(self, fixture_label):
        """
        Finds fixture files for a given label.
        """
        fixture_name, ser_fmt, cmp_fmt = self.parse_name(fixture_label)
        databases = [self.using, None]
        cmp_fmts = list(self.compression_formats.keys()
                        ) if cmp_fmt is None else [cmp_fmt]
        ser_fmts = serializers.get_public_serializer_formats() if ser_fmt is None else [
            ser_fmt]

        if self.verbosity >= 2:
            self.stdout.write("Loading '%s' fixtures..." % fixture_name)

        if os.path.isabs(fixture_name):
            fixture_dirs = [os.path.dirname(fixture_name)]
            fixture_name = os.path.basename(fixture_name)
        else:
            fixture_dirs = self.fixture_dirs
            if os.path.sep in os.path.normpath(fixture_name):
                fixture_dirs = [os.path.join(dir_, os.path.dirname(fixture_name))
                                for dir_ in fixture_dirs]
                fixture_name = os.path.basename(fixture_name)

        suffixes = ('.'.join(ext for ext in combo if ext)
                    for combo in product(databases, ser_fmts, cmp_fmts))
        targets = set('.'.join((fixture_name, suffix)) for suffix in suffixes)

        fixture_files = []
        for fixture_dir in fixture_dirs:
            if self.verbosity >= 2:
                self.stdout.write("Checking %s for fixtures..." %
                                  humanize(fixture_dir))
            fixture_files_in_dir = []
            for candidate in glob.iglob(os.path.join(fixture_dir, fixture_name + '*')):
                if os.path.basename(candidate) in targets:
                    # Save the fixture_dir and fixture_name for future error
                    # messages.
                    fixture_files_in_dir.append(
                        (candidate, fixture_dir, fixture_name))

            if self.verbosity >= 2 and not fixture_files_in_dir:
                self.stdout.write("No fixture '%s' in %s." %
                                  (fixture_name, humanize(fixture_dir)))

            # Check kept for backwards-compatibility; it isn't clear why
            # duplicates are only allowed in different directories.
            if len(fixture_files_in_dir) > 1:
                raise CommandError(
                    "Multiple fixtures named '%s' in %s. Aborting." %
                    (fixture_name, humanize(fixture_dir)))
            fixture_files.extend(fixture_files_in_dir)

        if fixture_name != 'initial_data' and not fixture_files:
            # Warning kept for backwards-compatibility; why not an exception?
            warnings.warn("No fixture named '%s' found." % fixture_name)
        elif fixture_name == 'initial_data' and fixture_files:
            warnings.warn(
                'initial_data fixtures are deprecated. Use data migrations instead.',
                RemovedInDjango19Warning
            )

        return fixture_files

    @cached_property
    def fixture_dirs(self):
        """
        Return a list of fixture directories.

        The list contains the 'fixtures' subdirectory of each installed
        application, if it exists, the directories in FIXTURE_DIRS, and the
        current directory.
        """
        dirs = ['/srv/leonardo/sites/leonardo/backup']
        fixture_dirs = settings.FIXTURE_DIRS
        if len(fixture_dirs) != len(set(fixture_dirs)):
            raise ImproperlyConfigured(
                "settings.FIXTURE_DIRS contains duplicates.")
        for app_config in apps.get_app_configs():
            app_label = app_config.label
            app_dir = os.path.join(app_config.path, 'fixtures')
            if app_dir in fixture_dirs:
                raise ImproperlyConfigured(
                    "'%s' is a default fixture directory for the '%s' app "
                    "and cannot be listed in settings.FIXTURE_DIRS." % (
                        app_dir, app_label)
                )

            if self.app_label and app_label != self.app_label:
                continue
            if os.path.isdir(app_dir):
                dirs.append(app_dir)
        dirs.extend(list(fixture_dirs))
        dirs.append('')
        dirs = [upath(os.path.abspath(os.path.realpath(d))) for d in dirs]
        return dirs

    def parse_name(self, fixture_name):
        """
        Splits fixture name in name, serialization format, compression format.
        """
        parts = fixture_name.rsplit('.', 2)

        if len(parts) > 1 and parts[-1] in self.compression_formats:
            cmp_fmt = parts[-1]
            parts = parts[:-1]
        else:
            cmp_fmt = None

        if len(parts) > 1:
            if parts[-1] in self.serialization_formats:
                ser_fmt = parts[-1]
                parts = parts[:-1]
            else:
                raise CommandError(
                    "Problem installing fixture '%s': %s is not a known "
                    "serialization format." % (''.join(parts[:-1]), parts[-1]))
        else:
            ser_fmt = None

        name = '.'.join(parts)

        return name, ser_fmt, cmp_fmt


class SingleZipReader(zipfile.ZipFile):

    def __init__(self, *args, **kwargs):
        zipfile.ZipFile.__init__(self, *args, **kwargs)
        if len(self.namelist()) != 1:
            raise ValueError("Zip-compressed fixtures must contain one file.")

    def read(self):
        return zipfile.ZipFile.read(self, self.namelist()[0])


def humanize(dirname):
    return "'%s'" % dirname if dirname else 'absolute path'