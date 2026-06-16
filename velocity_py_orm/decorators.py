__author__ = "sandeepkumarjakkaraju"

class Column:
    def __init__(self, name=None, field_type=None, primary_key=False, generated=False,
                 nullable=True, unique=False, length=255, encrypted=False,
                 version=False, created_at=False, updated_at=False, transient=False, ignore=False):
        self.name = name          # Database column name
        self.field_name = None    # Python class attribute name
        self.field_type = field_type
        self.primary_key = primary_key
        self.generated = generated
        self.nullable = nullable
        self.unique = unique
        self.length = length
        self.encrypted = encrypted
        self.version = version
        self.created_at = created_at
        self.updated_at = updated_at
        self.transient = transient
        self.ignore = ignore

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return instance.__dict__.get(self.field_name, None)

    def __set__(self, instance, value):
        instance.__dict__[self.field_name] = value


class EntityMeta:
    def __init__(self, entity_class, columns, id_column, table_name):
        self.entity_class = entity_class
        self.columns = columns
        self.id_column = id_column
        self.table_name = table_name

    def get_column_by_field_name(self, name):
        for col in self.columns:
            if col.field_name == name:
                return col
        return None

    def get_column_by_db_name(self, name):
        for col in self.columns:
            if col.name == name:
                return col
        return None


def entity(cls):
    columns = []
    id_column = None

    # Scan for Column instances
    for name, attr in list(cls.__dict__.items()):
        if isinstance(attr, Column):
            attr.field_name = name
            if not attr.name:
                attr.name = name
            if not attr.field_type:
                annotations = getattr(cls, '__annotations__', {})
                attr.field_type = annotations.get(name, str)
            
            if not attr.transient and not attr.ignore:
                columns.append(attr)
                if attr.primary_key:
                    id_column = attr

    # Fallback to field named 'id' as primary key if not specified
    if not id_column:
        for col in columns:
            if col.field_name == 'id':
                col.primary_key = True
                id_column = col
                break

    table_name = getattr(cls, '__table_name__', cls.__name__.lower() + "s")
    cls._meta = EntityMeta(cls, columns, id_column, table_name)
    
    # Add a default constructor that accepts keyword args if not already defined
    if '__init__' not in cls.__dict__:
        def default_init(self, **kwargs):
            for col in columns:
                setattr(self, col.field_name, kwargs.get(col.field_name, None))
        cls.__init__ = default_init

    return cls


def table(name):
    def decorator(cls):
        cls.__table_name__ = name
        if hasattr(cls, '_meta'):
            cls._meta.table_name = name
        return cls
    return decorator


def id(col):
    col.primary_key = True
    return col


def generated_value(col):
    col.generated = True
    return col


def encrypted(col):
    col.encrypted = True
    return col
