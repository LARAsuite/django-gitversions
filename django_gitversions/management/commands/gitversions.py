
from collections import OrderedDict
from datetime import datetime
from django.apps import apps
from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS
from django_gitversions import versioner
from django_gitversions.utils import get_queryset


class Command(BaseCommand):
    help = ("Make initial database dump.")

    def add_arguments(self, parser):
        parser.add_argument('args', metavar='app_label[.ModelName]', nargs='*',
                            help='Restricts dumped data to the specified app_label or app_label.ModelName.')
        parser.add_argument('--format', default='xml', dest='format',
                            help='Specifies the output serialization format for fixtures.')
        parser.add_argument('--indent', default=4, dest='indent', type=int,
                            help='Specifies the indent level to use when pretty-printing output.')
        parser.add_argument('--database', action='store', dest='database',
                            default=DEFAULT_DB_ALIAS,
                            help='Nominates a specific database to dump fixtures from. '
                            'Defaults to the "default" database.')
        parser.add_argument('-e', '--exclude', dest='exclude', action='append', default=[],
                            help='An app_label or app_label.ModelName to exclude '
                            '(use multiple --exclude to exclude multiple apps/models).')
        parser.add_argument('-n', '--natural', action='store_true', dest='use_natural_keys', default=True,
                            help='Use natural keys if they are available (deprecated: use --natural-foreign instead).')
        parser.add_argument('--natural-foreign', action='store_true', dest='use_natural_foreign_keys', default=True,
                            help='Use natural foreign keys if they are available.')
        parser.add_argument('--natural-primary', action='store_true', dest='use_natural_primary_keys', default=True,
                            help='Use natural primary keys if they are available.')
        parser.add_argument('-a', '--all', action='store_true', dest='use_base_manager', default=False,
                            help="Use Django's base manager to dump all models stored in the database, "
                            "including those that would otherwise be filtered or modified by a custom manager.")
        parser.add_argument('-o', '--output', default=None, dest='output',
                            help='Specifies file to which the output is written.')

        parser.add_argument('-c', '--commit', action='store_true', dest='use_base_manager', default=None,
                            help='Commit changes after complete dump.')
        parser.add_argument('-p', '--push', action='store_true', dest='use_base_manager', default=None,
                            help='Push to remote after complete dump.')

    def handle(self, *app_labels, **options):
        format = options.get('format', 'xml')
        indent = options.get('indent', 4)
        push = options.get('push', False)
        commit = options.get('commit', False)
        using = options.get('database')
        excludes = options.get('exclude', [])
        use_natural_keys = options.get('use_natural_keys', True)
        use_natural_foreign_keys = options.get(
            'use_natural_foreign_keys') or use_natural_keys
        use_natural_primary_keys = options.get(
            'use_natural_primary_keys', True)
        use_base_manager = options.get('use_base_manager')
        pks = options.get('primary_keys')

        if pks:
            primary_keys = pks.split(',')
        else:
            primary_keys = []

        excluded_apps = set()
        excluded_models = set()
        for exclude in excludes:
            if '.' in exclude:
                try:
                    model = apps.get_model(exclude)
                except LookupError:
                    raise CommandError(
                        'Unknown model in excludes: %s' % exclude)
                excluded_models.add(model)
            else:
                try:
                    app_config = apps.get_app_config(exclude)
                except LookupError:
                    raise CommandError('Unknown app in excludes: %s' % exclude)
                excluded_apps.add(app_config)

        if len(app_labels) == 0:
            if primary_keys:
                raise CommandError(
                    "You can only use --pks option with one model")
            app_list = OrderedDict((app_config, None)
                                   for app_config in apps.get_app_configs()
                                   if app_config.models_module is not None and app_config not in excluded_apps)
        else:
            if len(app_labels) > 1 and primary_keys:
                raise CommandError(
                    "You can only use --pks option with one model")
            app_list = OrderedDict()
            for label in app_labels:
                try:
                    app_label, model_label = label.split('.')
                    try:
                        app_config = apps.get_app_config(app_label)
                    except LookupError:
                        raise CommandError(
                            "Unknown application: %s" % app_label)
                    if app_config.models_module is None or app_config in excluded_apps:
                        continue
                    try:
                        model = app_config.get_model(model_label)
                    except LookupError:
                        raise CommandError("Unknown model: %s.%s" %
                                           (app_label, model_label))

                    app_list_value = app_list.setdefault(app_config, [])

                    # We may have previously seen a "all-models" request for
                    # this app (no model qualifier was given). In this case
                    # there is no need adding specific models to the list.
                    if app_list_value is not None:
                        if model not in app_list_value:
                            app_list_value.append(model)
                except ValueError:
                    if primary_keys:
                        raise CommandError(
                            "You can only use --pks option with one model")
                    # This is just an app - no model qualifier
                    app_label = label
                    try:
                        app_config = apps.get_app_config(app_label)
                    except LookupError:
                        raise CommandError(
                            "Unknown application: %s" % app_label)
                    if app_config.models_module is None or app_config in excluded_apps:
                        continue
                    app_list[app_config] = None

        # Check that the serialization format exists; this is a shortcut to
        # avoid collating all the objects and _then_ failing.
        if format not in serializers.get_public_serializer_formats():
            try:
                serializers.get_serializer(format)
            except serializers.SerializerDoesNotExist:
                pass

            raise CommandError("Unknown serialization format: %s" % format)

        # get all model classes

        models = 0
        instances = 0

        for model in sort_dependencies(app_list.items()):

            models += 1

            # get all objects
            queryset = get_queryset(
                model, using, primary_keys, use_base_manager)

            versioner.handle(queryset,
                             model=model,
                             format=format,
                             autocommit=False,
                             **{'indent': indent,
                                 'use_natural_foreign_keys': use_natural_foreign_keys,
                                'use_natural_primary_keys': use_natural_primary_keys,
                                })
            instances += queryset.count()

        self.stdout.write('Dumped {} applications, {} models and {} instances.'.format(
            len(app_list), models, instances))

        # commit & push to remote
        if commit or push:
            self.stdout.write('Commit & Push ...')
            versioner.backend.commit(
                'Initial versions from: {}'.format(datetime.now()), push=push)


def sort_dependencies(app_list):
    """Sort a list of (app_config, models) pairs into a single list of models.
    The single list of models is sorted so that any model with a natural key
    is serialized before a normal model, and any model with a natural key
    dependency has it's dependencies serialized first.
    """
    # Process the list of models, and get the list of dependencies
    model_dependencies = []
    models = set()
    for app_config, model_list in app_list:
        if model_list is None:
            model_list = app_config.get_models()

        for model in model_list:
            models.add(model)
            # Add any explicitly defined dependencies
            if hasattr(model, 'natural_key'):
                deps = getattr(model.natural_key, 'dependencies', [])
                if deps:
                    deps = [apps.get_model(dep) for dep in deps]
            else:
                deps = []

            # Now add a dependency for any FK or M2M relation with
            # a model that defines a natural key
            for field in model._meta.fields:
                if hasattr(field.rel, 'to'):
                    rel_model = field.rel.to
                    if hasattr(rel_model, 'natural_key') and rel_model != model:
                        deps.append(rel_model)
            for field in model._meta.many_to_many:
                rel_model = field.rel.to
                if hasattr(rel_model, 'natural_key') and rel_model != model:
                    deps.append(rel_model)
            model_dependencies.append((model, deps))

    model_dependencies.reverse()
    # Now sort the models to ensure that dependencies are met. This
    # is done by repeatedly iterating over the input list of models.
    # If all the dependencies of a given model are in the final list,
    # that model is promoted to the end of the final list. This process
    # continues until the input list is empty, or we do a full iteration
    # over the input models without promoting a model to the final list.
    # If we do a full iteration without a promotion, that means there are
    # circular dependencies in the list.
    model_list = []
    while model_dependencies:
        skipped = []
        changed = False
        while model_dependencies:
            model, deps = model_dependencies.pop()

            # If all of the models in the dependency list are either already
            # on the final model list, or not on the original serialization list,
            # then we've found another model with all it's dependencies
            # satisfied.
            found = True
            for candidate in ((d not in models or d in model_list) for d in deps):
                if not candidate:
                    found = False
            if found:
                model_list.append(model)
                changed = True
            else:
                skipped.append((model, deps))
        if not changed:
            raise CommandError("Can't resolve dependencies for %s in serialized app list." %
                               ', '.join('%s.%s' % (model._meta.app_label, model._meta.object_name)
                                         for model, deps in sorted(skipped, key=lambda obj: obj[0].__name__))
                               )
        model_dependencies = skipped

    return model_list
