"""
Lightweight schema migrations.

NOTE: Currently tested with SQLite and Postgresql. MySQL may be missing some
features.

Example Usage
-------------

Instantiate a migrator:

    # Postgres example:
    my_db = PostgresqlDatabase(...)
    migrator = PostgresqlMigrator(my_db)

    # SQLite example:
    my_db = SqliteDatabase('my_database.db')
    migrator = SqliteMigrator(my_db)

Then you will use the `migrate` function to run various `Operation`s which
are generated by the migrator:

    migrate(
        migrator.add_column('some_table', 'column_name', CharField(default=''))
    )

Migrations are not run inside a transaction, so if you wish the migration to
run in a transaction you will need to wrap the call to `migrate` in a
transaction block, e.g.:

    with my_db.transaction():
        migrate(...)

Supported Operations
--------------------

Add new field(s) to an existing model:

    # Create your field instances. For non-null fields you must specify a
    # default value.
    pubdate_field = DateTimeField(null=True)
    comment_field = TextField(default='')

    # Run the migration, specifying the database table, field name and field.
    migrate(
        migrator.add_column('comment_tbl', 'pub_date', pubdate_field),
        migrator.add_column('comment_tbl', 'comment', comment_field),
    )

Renaming a field:

    # Specify the table, original name of the column, and its new name.
    migrate(
        migrator.rename_column('story', 'pub_date', 'publish_date'),
        migrator.rename_column('story', 'mod_date', 'modified_date'),
    )

Dropping a field:

    migrate(
        migrator.drop_column('story', 'some_old_field'),
    )

Making a field nullable or not nullable:

    # Note that when making a field not null that field must not have any
    # NULL values present.
    migrate(
        # Make `pub_date` allow NULL values.
        migrator.drop_not_null('story', 'pub_date'),

        # Prevent `modified_date` from containing NULL values.
        migrator.add_not_null('story', 'modified_date'),
    )

Renaming a table:

    migrate(
        migrator.rename_table('story', 'stories_tbl'),
    )

Adding an index:

    # Specify the table, column names, and whether the index should be
    # UNIQUE or not.
    migrate(
        # Create an index on the `pub_date` column.
        migrator.add_index('story', ('pub_date',), False),

        # Create a multi-column index on the `pub_date` and `status` fields.
        migrator.add_index('story', ('pub_date', 'status'), False),

        # Create a unique index on the category and title fields.
        migrator.add_index('story', ('category_id', 'title'), True),
    )

Dropping an index:

    # Specify the index name.
    migrate(migrator.drop_index('story_pub_date_status'))
"""
from collections import namedtuple
import functools
import re

from peewee import *
from peewee import CommaClause
from peewee import EnclosedClause
from peewee import Entity
from peewee import Expression
from peewee import Node
from peewee import OP_EQ


class Operation(object):
    """Encapsulate a single schema altering operation."""
    def __init__(self, migrator, method, *args, **kwargs):
        self.migrator = migrator
        self.method = method
        self.args = args
        self.kwargs = kwargs

    def _parse_node(self, node):
        compiler = self.migrator.database.compiler()
        return compiler.parse_node(node)

    def execute(self, node):
        sql, params = self._parse_node(node)
        self.migrator.database.execute_sql(sql, params)

    def _handle_result(self, result):
        if isinstance(result, Node):
            self.execute(result)
        elif isinstance(result, Operation):
            result.run()
        elif isinstance(result, (list, tuple)):
            for item in result:
                self._handle_result(item)

    def run(self):
        kwargs = self.kwargs.copy()
        kwargs['generate'] = True
        self._handle_result(
            getattr(self.migrator, self.method)(*self.args, **kwargs))


def operation(fn):
    @functools.wraps(fn)
    def inner(self, *args, **kwargs):
        generate = kwargs.pop('generate', False)
        if generate:
            return fn(self, *args, **kwargs)
        return Operation(self, fn.__name__, *args, **kwargs)
    return inner

class SchemaMigrator(object):
    def __init__(self, database):
        self.database = database

    @classmethod
    def from_database(cls, database):
        if isinstance(database, PostgresqlDatabase):
            return PostgresqlMigrator(database)
        elif isinstance(database, MySQLDatabase):
            return MySQLMigrator(database)
        else:
            return SqliteMigrator(database)

    @operation
    def apply_default(self, table, column_name, field):
        default = field.default
        if callable(default):
            default = default()

        return Clause(
            SQL('UPDATE'),
            Entity(table),
            SQL('SET'),
            Expression(
                Entity(column_name),
                OP_EQ,
                Param(field.db_value(default)),
                flat=True))

    @operation
    def alter_add_column(self, table, column_name, field):
        # Make field null at first.
        field_null, field.null = field.null, True
        field.name = field.db_column = column_name
        field_clause = self.database.compiler().field_definition(field)
        field.null = field_null
        return Clause(
            SQL('ALTER TABLE'),
            Entity(table),
            SQL('ADD COLUMN'),
            field_clause)

    @operation
    def add_column(self, table, column_name, field):
        # Adding a column is complicated by the fact that if there are rows
        # present and the field is non-null, then we need to first add the
        # column as a nullable field, then set the value, then add a not null
        # constraint.
        if not field.null and field.default is None:
            raise ValueError('%s is not null but has no default' % column_name)

        # Foreign key fields must explicitly specify a `to_field`.
        if isinstance(field, ForeignKeyField) and not field.to_field:
            raise ValueError('Foreign keys must specify a `to_field`.')

        operations = [self.alter_add_column(table, column_name, field)]

        # In the event the field is *not* nullable, update with the default
        # value and set not null.
        if not field.null:
            operations.extend([
                self.apply_default(table, column_name, field),
                self.add_not_null(table, column_name)])

        return operations

    @operation
    def drop_column(self, table, column_name, cascade=True):
        nodes = [
            SQL('ALTER TABLE'),
            Entity(table),
            SQL('DROP COLUMN'),
            Entity(column_name)]
        if cascade:
            nodes.append(SQL('CASCADE'))
        return Clause(*nodes)

    @operation
    def rename_column(self, table, old_name, new_name):
        return Clause(
            SQL('ALTER TABLE'),
            Entity(table),
            SQL('RENAME COLUMN'),
            Entity(old_name),
            SQL('TO'),
            Entity(new_name))

    def _alter_column(self, table, column):
        return [
            SQL('ALTER TABLE'),
            Entity(table),
            SQL('ALTER COLUMN'),
            Entity(column)]

    @operation
    def add_not_null(self, table, column):
        nodes = self._alter_column(table, column)
        nodes.append(SQL('SET NOT NULL'))
        return Clause(*nodes)

    @operation
    def drop_not_null(self, table, column):
        nodes = self._alter_column(table, column)
        nodes.append(SQL('DROP NOT NULL'))
        return Clause(*nodes)

    @operation
    def rename_table(self, old_name, new_name):
        return Clause(
            SQL('ALTER TABLE'),
            Entity(old_name),
            SQL('RENAME TO'),
            Entity(new_name))

    @operation
    def add_index(self, table, columns, unique=False):
        compiler = self.database.compiler()
        statement = 'CREATE UNIQUE INDEX' if unique else 'CREATE INDEX'
        return Clause(
            SQL(statement),
            Entity(compiler.index_name(table, columns)),
            SQL('ON'),
            Entity(table),
            EnclosedClause(*[Entity(column) for column in columns]))

    @operation
    def drop_index(self, table, index_name):
        return Clause(
            SQL('DROP INDEX'),
            Entity(index_name))


class PostgresqlMigrator(SchemaMigrator):
    def _primary_key_columns(self, tbl):
        query = """
            SELECT pg_attribute.attname
            FROM pg_index, pg_class, pg_attribute
            WHERE
                pg_class.oid = '%s'::regclass AND
                indrelid = pg_class.oid AND
                pg_attribute.attrelid = pg_class.oid AND
                pg_attribute.attnum = any(pg_index.indkey) AND
                indisprimary;
        """
        cursor = self.database.execute_sql(query % tbl)
        return [row[0] for row in cursor.fetchall()]

    @operation
    def rename_table(self, old_name, new_name):
        pk_names = self._primary_key_columns(old_name)
        ParentClass = super(PostgresqlMigrator, self)

        operations = [
            ParentClass.rename_table(old_name, new_name, generate=True)]

        if len(pk_names) == 1:
            # Check for existence of primary key sequence.
            seq_name = '%s_%s_seq' % (old_name, pk_names[0])
            query = """
                SELECT 1
                FROM information_schema.sequences
                WHERE sequence_name = %s
            """
            cursor = self.database.execute_sql(query, (seq_name,))
            if bool(cursor.fetchone()):
                new_seq_name = '%s_%s_seq' % (new_name, pk_names[0])
                operations.append(ParentClass.rename_table(
                    seq_name, new_seq_name, generate=True))

        return operations

_column_attributes = ('name', 'definition', 'null', 'pk', 'default', 'extra')

class MySQLColumn(namedtuple('_Column', _column_attributes)):
    @property
    def is_pk(self):
        return self.pk == 'PRI'

    @property
    def is_unique(self):
        return self.pk == 'UNI'

    @property
    def is_null(self):
        return self.null == 'YES'

    def sql(self, column_name=None, is_null=None):
        if is_null is None:
            is_null = self.is_null
        if column_name is None:
            column_name = self.name
        parts = [
            Entity(column_name),
            SQL(self.definition)]
        if self.is_unique:
            parts.append(SQL('UNIQUE'))
        if not is_null:
            parts.append(SQL('NOT NULL'))
        if self.is_pk:
            parts.append(SQL('PRIMARY KEY'))
        if self.extra:
            parts.append(SQL(extra))
        return Clause(*parts)


class MySQLMigrator(SchemaMigrator):
    def _get_column_definition(self, table, column_name):
        cursor = self.database.execute_sql('DESCRIBE %s;' % table)
        rows = cursor.fetchall()
        for row in rows:
            column = MySQLColumn(*row)
            if column.name == column_name:
                return column
        return False

    @operation
    def add_not_null(self, table, column):
        column = self._get_column_definition(table, column)
        return Clause(
            SQL('ALTER TABLE'),
            Entity(table),
            SQL('MODIFY'),
            column.sql(is_null=False))

    @operation
    def drop_not_null(self, table, column):
        column = self._get_column_definition(table, column)
        return Clause(
            SQL('ALTER TABLE'),
            Entity(table),
            SQL('MODIFY'),
            column.sql(is_null=True))

    @operation
    def rename_column(self, table, old_name, new_name):
        column = self._get_column_definition(table, old_name)
        return Clause(
            SQL('ALTER TABLE'),
            Entity(table),
            SQL('CHANGE'),
            Entity(old_name),
            column.sql(column_name=new_name))

    @operation
    def drop_index(self, table, index_name):
        return Clause(
            SQL('DROP INDEX'),
            Entity(index_name),
            SQL('ON'),
            Entity(table))


_IndexMetadata = namedtuple('_IndexMetadata', ('name', 'sql', 'columns'))

class SqliteMigrator(SchemaMigrator):
    """
    SQLite supports a subset of ALTER TABLE queries, view the docs for the
    full details http://sqlite.org/lang_altertable.html
    """
    column_re = re.compile('(.+?)\((.+)\)')
    column_split_re = re.compile(r'(?:[^,(]|\([^)]*\))+')
    column_name_re = re.compile('"?([\w]+)')

    def _get_column_names(self, table):
        res = self.database.execute_sql('select * from "%s" limit 1' % table)
        return [item[0] for item in res.description]

    def _get_create_table(self, table):
        res = self.database.execute_sql(
            'select sql from sqlite_master where type=? and name=? limit 1',
            ['table', table])
        return res.fetchone()[0]

    def _get_indexes(self, table):
        cursor = self.database.execute_sql(
            'SELECT name, sql FROM sqlite_master '
            'WHERE tbl_name = ? AND type = ?',
            [table, 'index'])
        index_to_sql = dict(cursor.fetchall())
        indexed_columns = {}
        for index_name in sorted(index_to_sql):
            cursor = self.database.execute_sql(
                'PRAGMA index_info("%s")' % index_name)
            indexed_columns[index_name] = [row[2] for row in cursor.fetchall()]

        return [_IndexMetadata(key, index_to_sql[key], indexed_columns[key])
                for key in sorted(index_to_sql)]

    @operation
    def _update_column(self, table, column_to_update, fn):
        # Get the SQL used to create the given table.
        create_table = self._get_create_table(table)

        # Get the indexes and SQL to re-create indexes.
        indexes = self._get_indexes(table)

        # Parse out the `CREATE TABLE` and column list portions of the query.
        raw_create, raw_columns = self.column_re.search(create_table).groups()

        # Clean up the individual column definitions.
        column_defs = [
            col.strip() for col in self.column_split_re.findall(raw_columns)]

        new_column_defs = []
        new_column_names = []
        original_column_names = []

        for column_def in column_defs:
            column_name, = self.column_name_re.match(column_def).groups()

            if column_name == column_to_update:
                new_column_def = fn(column_name, column_def)
                if new_column_def:
                    new_column_defs.append(new_column_def)
                    original_column_names.append(column_name)
                    column_name, = self.column_name_re.match(
                        new_column_def).groups()
                    new_column_names.append(column_name)
            else:
                new_column_defs.append(column_def)
                if not column_name.lower().startswith(('foreign', 'primary')):
                    new_column_names.append(column_name)
                    original_column_names.append(column_name)

        # Update the name of the new CREATE TABLE query.
        temp_table = table + '__tmp__'
        create = re.sub(
            '("?)%s("?)' % table,
            '\\1%s\\2' % temp_table,
            raw_create)

        # Create the new table.
        columns = ', '.join(new_column_defs)
        queries = [
            Clause(SQL('DROP TABLE IF EXISTS'), Entity(temp_table)),
            SQL('%s (%s)' % (create.strip(), columns))]

        # Populate new table.
        populate_table = Clause(
            SQL('INSERT INTO'),
            Entity(temp_table),
            EnclosedClause(*[Entity(col) for col in new_column_names]),
            SQL('SELECT'),
            CommaClause(*[Entity(col) for col in original_column_names]),
            SQL('FROM'),
            Entity(table))
        queries.append(populate_table)

        # Drop existing table and rename temp table.
        queries.append(Clause(
            SQL('DROP TABLE'),
            Entity(table)))
        queries.append(self.rename_table(temp_table, table))

        # Re-create indexes.
        original_to_new = dict(zip(original_column_names, new_column_names))
        new_column = original_to_new.get(column_to_update)

        for index in indexes:
            if column_to_update in index.columns:
                if new_column:
                    queries.append(
                        SQL(index.sql.replace(column_to_update, new_column)))
            else:
                queries.append(SQL(index.sql))

        return queries

    @operation
    def drop_column(self, table, column_name, cascade=True):
        return self._update_column(table, column_name, lambda a, b: None)

    @operation
    def rename_column(self, table, old_name, new_name):
        def _rename(column_name, column_def):
            return column_def.replace(column_name, new_name)
        return self._update_column(table, old_name, _rename)

    @operation
    def add_not_null(self, table, column):
        def _add_not_null(column_name, column_def):
            return column_def + ' NOT NULL'
        return self._update_column(table, column, _add_not_null)

    @operation
    def drop_not_null(self, table, column):
        def _drop_not_null(column_name, column_def):
            return column_def.replace('NOT NULL', '')
        return self._update_column(table, column, _drop_not_null)


def migrate(*operations, **kwargs):
    for operation in operations:
        operation.run()
