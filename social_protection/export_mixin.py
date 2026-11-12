import json
import logging
import types


from core.custom_filters import CustomFilterWizardStorage
from core.models import ExportableQueryModel
from core.gql.export_mixin import ExportableQueryMixin
from django.core.exceptions import FieldDoesNotExist
from django.db.models import JSONField, QuerySet
from graphene_django.registry import get_global_registry

EXPORT_FIELD_NOT_ALLOWED_MESSAGE = "social_protection.export.field_not_allowed"

logger = logging.getLogger(__file__)


class ExportableSocialProtectionQueryMixin(ExportableQueryMixin):

    @classmethod
    def create_export_function(cls, field_name):
        new_function_name = f"resolve_{field_name}_export"
        default_resolve = getattr(cls, F"resolve_{field_name}", None)

        if not default_resolve:
            raise AttributeError(
                f"Query {cls} doesn't provide resolve function for {field_name}. "
                f"CSV export cannot be created")

        def exporter(cls, self, info, **kwargs):
            custom_filters = kwargs.pop("customFilters", None)
            export_fields = [cls._adjust_notation(f) for f in kwargs.pop('fields')]
            fields_mapping = json.loads(kwargs.pop('fields_columns'))

            source_field = getattr(cls, field_name)
            filter_kwargs = {k: v for k, v in kwargs.items() if k in source_field.filtering_args}

            qs = cls._exportable_queryset(default_resolve(None, info, **kwargs), export_fields, info)
            qs = qs.filter(**filter_kwargs)
            qs = cls.__append_custom_filters(custom_filters, qs, fields_mapping)
            export_file = ExportableQueryModel\
                .create_csv_export(qs, export_fields, info.context.user, column_names=fields_mapping,
                                   patches=cls.get_patches_for_field(field_name))

            return export_file.name

        setattr(cls, new_function_name, types.MethodType(exporter, cls))

    @classmethod
    def __append_custom_filters(cls, custom_filters, queryset, fields_mapping):
        if custom_filters:
            module_name = cls.get_module_name()
            object_type = cls.get_object_type()
            related_field = cls.get_related_field()
            if "group__id" in fields_mapping:
                queryset = CustomFilterWizardStorage.build_custom_filters_queryset(
                    "individual",
                    "GroupIndividual",
                    custom_filters,
                    queryset,
                    relation="group"
                )
            else:
                queryset = CustomFilterWizardStorage.build_custom_filters_queryset(
                    module_name,
                    object_type,
                    custom_filters,
                    queryset,
                    relation=related_field
                )
        return queryset

    @classmethod
    def _exportable_queryset(cls, queryset, export_fields, info):
        """
        Checks the exported field paths and filters the rows through the
        get_queryset of the GraphQL type registered for the queryset's model,
        so an export returns what the query field returns.
        """
        if not isinstance(queryset, QuerySet):
            return queryset
        for path in export_fields:
            cls._assert_exportable_path(queryset.model, path)
        node_type = get_global_registry().get_type_for_model(queryset.model)
        return node_type.get_queryset(queryset, info) if node_type else queryset

    @staticmethod
    def _assert_exportable_path(model, path):
        """
        Exported paths follow local fields and forward relations only; a single
        unknown name is left to the queryset (annotation), and the segments after
        a JSON field are keys inside it.
        """
        segments = path.split("__")
        current = model
        for index, segment in enumerate(segments):
            try:
                field = current._meta.get_field(segment)
            except FieldDoesNotExist:
                if len(segments) == 1:
                    return
                raise ValueError(f"{EXPORT_FIELD_NOT_ALLOWED_MESSAGE}: {path}")
            if not field.concrete or field.many_to_many:
                raise ValueError(f"{EXPORT_FIELD_NOT_ALLOWED_MESSAGE}: {path}")
            if field.is_relation:
                current = field.related_model
                continue
            if isinstance(field, JSONField) or index == len(segments) - 1:
                return
            raise ValueError(f"{EXPORT_FIELD_NOT_ALLOWED_MESSAGE}: {path}")
