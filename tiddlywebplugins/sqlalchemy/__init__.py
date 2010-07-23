"""
Yet another attempt to get a good sql store.
"""
import logging

from tiddlyweb import __version__ as VERSION

from base64 import b64encode, b64decode
from sqlalchemy import select, desc
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import relation, mapper, sessionmaker, scoped_session
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.schema import (Table, Column, PrimaryKeyConstraint,
        UniqueConstraint, ForeignKeyConstraint, Index, MetaData)
from sqlalchemy.sql import func
from sqlalchemy.sql.expression import and_, text as text_
from sqlalchemy.types import Unicode, Integer, String, UnicodeText, CHAR
from tiddlyweb.model.bag import Bag
from tiddlyweb.model.policy import Policy
from tiddlyweb.model.recipe import Recipe
from tiddlyweb.model.tiddler import Tiddler, string_to_tags_list
from tiddlyweb.model.user import User
from tiddlyweb.serializer import Serializer
from tiddlyweb.store import (NoBagError, NoRecipeError, NoTiddlerError,
        NoUserError)
from tiddlyweb.stores import StorageInterface
from tiddlyweb.util import binary_tiddler

#logging.basicConfig()
#logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)

metadata = MetaData()
Session = scoped_session(sessionmaker())

field_table = Table('field', metadata,
    Column('revision_number', Integer, index=True, nullable=False),
    Column('name', Unicode(64), index=True, nullable=False),
    Column('value', Unicode(1024)),
    PrimaryKeyConstraint('revision_number', 'name'),
    ForeignKeyConstraint(['revision_number'],
                         ['revision.number'],
                         onupdate='CASCADE', ondelete='CASCADE'),
    )

revision_table = Table('revision', metadata,
    Column('bag_name', Unicode(128), index=True, nullable=False),
    Column('tiddler_title', Unicode(128), index=True, nullable=False),
    Column('number', Integer, primary_key=True, nullable=False,
        autoincrement=True),
    Column('modifier', Unicode(128)),
    Column('modified', String(14)),
    Column('type', String(128)),
    Column('tags', Unicode(1024)),
    Column('text', UnicodeText(16777215), nullable=False, default=u''),
    UniqueConstraint('bag_name', 'tiddler_title', 'number'),
    ForeignKeyConstraint(['bag_name', 'tiddler_title'],
                         ['bag.name'],
                         onupdate='CASCADE', ondelete='CASCADE'),
    )

bag_table = Table('bag', metadata,
    Column('name', Unicode(128), primary_key=True),
    Column('desc', Unicode(1024)),
    )

policy_table = Table('policy', metadata,
    Column('container_name', Unicode(128), nullable=False),
    Column('type', String(12), nullable=False),
    Column('principal_name', Unicode(128), index=True, nullable=False),
    Column('principal_type', CHAR(1), nullable=False),
    PrimaryKeyConstraint('container_name', 'type'),
    ForeignKeyConstraint(['principal_name', 'principal_type'],
        ['principal.name', 'principal.type'],
        onupdate='CASCADE', ondelete='CASCADE'),
    )

recipe_table = Table('recipe', metadata,
    Column('name', Unicode(128), primary_key=True, nullable=False),
    Column('desc', Unicode(1024)),
    Column('recipe_string', UnicodeText, default=u''),
    )

principal_table = Table('principal', metadata,
    Column('name', Unicode(128), nullable=False),
    Column('type', CHAR(1), nullable=False),
    PrimaryKeyConstraint('name', 'type'),
    )

role_table = Table('role', metadata,
    Column('user', Unicode(128), nullable=False),
    Column('name', Unicode(50), nullable=False),
    PrimaryKeyConstraint('user', 'name'),
    ForeignKeyConstraint(['user'], ['user.usersign'],
        onupdate='CASCADE', ondelete='CASCADE'),
    )

user_table = Table('user', metadata,
    Column('usersign', Unicode(128), primary_key=True, nullable=False),
    Column('note', Unicode(1024)),
    Column('password', String(128)),
    PrimaryKeyConstraint('usersign'),
    )


class sField(object):

    def __init__(self, name, value):
        object.__init__(self)
        self.name = name
        self.value = value

    def __repr__(self):
        return '<sField(%s:%s)>' % (self.name, self.value)


class sRevision(object):

    def __init__(self, title, bag_name, rev=0):
        object.__init__(self)
        self.tiddler_title = title
        self.bag_name = bag_name
        self.number = rev

    def __repr__(self):
        return '<sRevision(%s:%s:%d)>' % (self.bag_name,
                self.tiddler_title, self.number)


class sPolicy(object):

    def __repr__(self):
        return '<sPolicy(%s:%s:%s:%s)>' % (self.container_name,
                self.principal_type, self.principal_name, self.type)


class sBag(object):

    def __init__(self, name, desc=''):
        object.__init__(self)
        self.name = name
        self.desc = desc

    def __repr__(self):
        return '<sBag(%s)>' % (self.name)


class sRecipe(object):

    def __init__(self, name, desc=''):
        self.name = name
        self.desc = desc

    def __repr__(self):
        return '<sRecipe(%s)>' % (self.name)


class sPrincipal(object):

    def __repr__(self):
        return '<sPrincipal(%s:%s)>' % (self.type, self.name)


class sRole(object):

    def __repr__(self):
        return '<sRole(%s:%s)>' % (self.user, self.name)


class sUser(object):

    def __repr__(self):
        return '<sUser(%s)>' % (self.usersign)


mapper(sField, field_table)

mapper(sRevision, revision_table, properties=dict(
    fields=relation(sField,
        backref='revision',
        cascade='delete',
        lazy=True)))

mapper(sBag, bag_table, properties=dict(
    tiddlers=relation(sRevision,
        lazy = True,
        cascade='delete',
        primaryjoin=(revision_table.c.bag_name==bag_table.c.name)
        ),
    policy=relation(sPolicy,
        primaryjoin=(policy_table.c.container_name == bag_table.c.name),
        cascade='delete',
        foreign_keys=policy_table.c.container_name,
        lazy=False)))

mapper(sUser, user_table, properties=dict(
    roles=relation(sRole,
        lazy=False,
        cascade='delete')))

mapper(sPolicy, policy_table)

mapper(sRecipe, recipe_table, properties=dict(
    policy=relation(sPolicy,
        primaryjoin=(policy_table.c.container_name == recipe_table.c.name),
        cascade='delete',
        foreign_keys=policy_table.c.container_name,
        lazy=False)))

mapper(sRole, role_table)

mapper(sPrincipal, principal_table)


class Store(StorageInterface):
    """
    A SqlAlchemy based storage interface for TiddlyWeb.
    """

    mapped = False

    def __init__(self, store_config=None, environ=None):
        super(Store, self).__init__(store_config, environ)
        self.store_type = self._db_config().split(':', 1)[0]
        self._init_store()

    def _init_store(self):
        """
        Establish the database engine and session,
        creating tables if needed.
        """
        engine = create_engine(self._db_config())
        metadata.bind = engine
        Session.configure(bind=engine)
        self.session = Session()
        self.serializer = Serializer('text')

        if not Store.mapped:
            metadata.create_all(engine)
            Store.mapped = True

    def _db_config(self):
        return self.store_config['db_config']

    def list_recipes(self):
        try:
            return (self._load_recipe(Recipe(srecipe.name), srecipe)
                    for srecipe in self.session.query(sRecipe).all())
        except:
            self.session.rollback()
            raise

    def list_bags(self):
        try:
            return (self._load_bag(Bag(sbag.name), sbag)
                    for sbag in self.session.query(sBag).all())
        except:
            self.session.rollback()
            raise

    def list_users(self):
        try:
            return (self._load_user(User(suser.usersign), suser)
                    for suser in self.session.query(sUser).all())
        except:
            self.session.rollback()
            raise

    def list_bag_tiddlers(self, bag):
        try:
            query = (self.session.query(sRevision,func.max(sRevision.number))
                    .filter(sRevision.bag_name==bag.name).group_by(sRevision.tiddler_title))
            try:
                sbag = self.session.query(sBag).filter(sBag.name
                        == bag.name).one()
            except NoResultFound, exc:
                raise NoBagError('no results for bag %s, %s' % (bag.name, exc))

            tiddlers = query.all()

            try:
                store = self.environ['tiddlyweb.store']
            except KeyError:
                store = bag.store

            def _bags_tiddler(stiddler):
                tiddler = Tiddler(stiddler.tiddler_title, bag.name)
                tiddler = self.tiddler_get(tiddler)
                return tiddler

            for stiddler, _ in tiddlers:
                if stiddler:
                    yield _bags_tiddler(stiddler)
        except:
            self.session.rollback()
            raise

    def list_tiddler_revisions(self, tiddler):
        try:
            query = (self.session.query(sRevision.number)
                    .filter(revision_table.c.tiddler_title == tiddler.title)
                    .filter(revision_table.c.bag_name == tiddler.bag)
                    .order_by(sRevision.number.desc()))
            revisions = query.all()
            if not revisions:
                raise NoTiddlerError('tiddler %s not found' % (tiddler.title,))
            else:
                return [revision[0] for revision in revisions]
        except:
            self.session.rollback()
            raise

    def recipe_delete(self, recipe):
        try:
            try:
                srecipe = self.session.query(sRecipe).filter(sRecipe.name
                        == recipe.name).one()
                self.session.delete(srecipe)
                self.session.commit()
            except NoResultFound, exc:
                raise NoRecipeError('no results for recipe %s, %s' %
                        (recipe.name, exc))
        except:
            self.session.rollback()
            raise

    def recipe_get(self, recipe):
        try:
            try:
                srecipe = self.session.query(sRecipe).filter(sRecipe.name
                        == recipe.name).one()
                recipe = self._load_recipe(recipe, srecipe)
                return recipe
            except NoResultFound, exc:
                raise NoRecipeError('no results for recipe %s, %s' %
                        (recipe.name, exc))
        except:
            self.session.rollback()
            raise

    def recipe_put(self, recipe):
        try:
            srecipe = self._store_recipe(recipe)
            self.session.merge(srecipe)
            self.session.commit()
        except:
            self.session.rollback()
            raise

    def bag_delete(self, bag):
        try:
            try:
                sbag = self.session.query(sBag).filter(sBag.name
                        == bag.name).one()
                self.session.delete(sbag)
                self.session.commit()
            except NoResultFound, exc:
                raise NoBagError('Bag %s not found: %s' % (bag.name, exc))
        except:
            self.session.rollback()
            raise

    def bag_get(self, bag):
        try:
            try:
                sbag = self.session.query(sBag).filter(sBag.name
                        == bag.name).one()
                bag = self._load_bag(bag, sbag)
                if VERSION.startswith('1.0'):
                    if not (hasattr(bag, 'skinny') and bag.skinny):
                        bag.add_tiddlers(self.list_bag_tiddlers(bag))
                return bag
            except NoResultFound, exc:
                raise NoBagError('Bag %s not found: %s' % (bag.name, exc))
        except:
            self.session.rollback()
            raise

    def bag_put(self, bag):
        try:
            sbag = self._store_bag(bag)
            self.session.merge(sbag)
            self.session.commit()
        except:
            self.session.rollback()
            raise

    def tiddler_delete(self, tiddler):
        try:
            try:
                stiddler = (self.session.query(sRevision).
                        filter(sRevision.tiddler_title == tiddler.title).
                        filter(sRevision.bag_name == tiddler.bag))
                rows = stiddler.delete()
                if rows == 0:
                    raise NoResultFound
                self.session.commit()
                self.tiddler_written(tiddler)
            except NoResultFound, exc:
                raise NoTiddlerError('no tiddler %s to delete, %s' %
                        (tiddler.title, exc))
        except:
            self.session.rollback()
            raise

    def tiddler_get(self, tiddler):
        try:
            try:
                query = (self.session.query(sRevision).
                        filter(sRevision.tiddler_title == tiddler.title).
                        filter(sRevision.bag_name == tiddler.bag))
                base_tiddler = query.order_by(sRevision.number.asc()).limit(1)
                if tiddler.revision:
                    query = query.filter(sRevision.number == tiddler.revision)
                else:
                    query = query.order_by(sRevision.number.desc()).limit(1)
                stiddler = query.one()
                base_tiddler = base_tiddler.one()
                tiddler = self._load_tiddler(tiddler, stiddler, base_tiddler)
                return tiddler
            except NoResultFound, exc:
                raise NoTiddlerError('Tiddler %s not found: %s' %
                        (tiddler.title, exc))
        except:
            self.session.rollback()
            raise

    def tiddler_put(self, tiddler):
        tiddler.revision = None
        try:
            if not tiddler.bag:
                raise NoBagError('bag required to save')
            stiddler = self._store_tiddler(tiddler)
            self.session.add(stiddler)
            tiddler.revision = stiddler.number
            self.session.commit()
            self.tiddler_written(tiddler)
            self.session.commit()
        except:
            self.session.rollback()
            raise

    def user_delete(self, user):
        try:
            try:
                suser = self.session.query(sUser).filter(sUser.usersign
                        == user.usersign).one()
                self.session.delete(suser)
                self.session.commit()
            except NoResultFound, exc:
                raise NoUserError('user %s not found, %s' %
                        (user.usersign, exc))
        except:
            self.session.rollback()
            raise

    def user_get(self, user):
        try:
            try:
                suser = self.session.query(sUser).filter(sUser.usersign
                        == user.usersign).one()
                user = self._load_user(user, suser)
                return user
            except NoResultFound, exc:
                raise NoUserError('user %s not found, %s' %
                        (user.usersign, exc))
        except:
            self.session.rollback()
            raise

    def user_put(self, user):
        try:
            suser = self._store_user(user)
            self.session.merge(suser)
            self._store_roles(user)
            self.session.commit()
        except:
            self.session.rollback()
            raise

    def _load_bag(self, bag, sbag):
        bag.desc = sbag.desc
        bag.policy = self._load_policy(sbag.policy)
        bag.store = True
        return bag

    def _load_policy(self, spolicy):
        policy = Policy()

        if spolicy is not None:
            for pol in spolicy:
                principal_name = pol.principal_name
                if pol.principal_type == 'R':
                    principal_name = 'R:%s' % pol.principal_name
                if pol.type == 'owner':
                    policy.owner = principal_name
                else:
                    principals = getattr(policy, pol.type, [])
                    principals.append(principal_name)
                    setattr(policy, pol.type, principals)
        return policy

    def _load_tiddler(self, tiddler, stiddler, base_tiddler):
        try:
            revision = stiddler

            tiddler.modifier = revision.modifier
            tiddler.modified = revision.modified
            tiddler.revision = revision.number
            tiddler.type = revision.type

            if (tiddler.type and tiddler.type != 'None' and not
                    tiddler.type.startswith('text/')):
                tiddler.text = b64decode(revision.text.lstrip().rstrip())
            else:
                tiddler.text = revision.text
            tiddler.tags = self._load_tags(revision.tags)

            for sfield in revision.fields:
                tiddler.fields[sfield.name] = sfield.value

            tiddler.created = base_tiddler.modified
            tiddler.creator = base_tiddler.modifier

            return tiddler
        except IndexError, exc:
            try:
                index_error = exc
                raise NoTiddlerError('No revision %s for tiddler %s, %s' %
                        (stiddler.rev, stiddler.title, exc))
            except AttributeError:
                raise NoTiddlerError('No tiddler for tiddler %s, %s' %
                        (stiddler.title, index_error))

    def _load_recipe(self, recipe, srecipe):
        recipe.desc = srecipe.desc
        recipe.policy = self._load_policy(srecipe.policy)
        recipe.set_recipe(self._load_recipe_string(srecipe.recipe_string))
        recipe.store = True
        return recipe

    def _load_recipe_string(self, recipe_string):
        recipe = []
        if recipe_string:
            for line in recipe_string.split('\n'):
                bag, filter = line.split('?', 1)
                recipe.append((bag, filter))
        return recipe

    def _load_tags(self, tags_string):
        return string_to_tags_list(tags_string)

    def _load_user(self, user, suser):
        user.usersign = suser.usersign
        user._password = suser.password
        user.note = suser.note
        [user.add_role(role.name) for role in suser.roles]
        return user

    def _store_bag(self, bag):
        sbag = sBag(bag.name, bag.desc)
        self._store_policy(bag.name, bag.policy)
        return sbag

    def _store_policy(self, container, policy):
        for attribute in policy.attributes:

            if attribute == 'owner':
                policy.owner = policy.owner is None and [] or [policy.owner]
            for principal_name in getattr(policy, attribute, []):
                if principal_name is not None:
                    spolicy = sPolicy()
                    spolicy.container_name = container
                    spolicy.type = attribute

                    if principal_name.startswith('R:'):
                        pname = principal_name[2:]
                        ptype = 'R'
                    else:
                        pname = principal_name
                        ptype = 'U'

                    try:
                        sprincipal = (self.session.query(sPrincipal).
                                    filter(sPrincipal.name == pname).
                                    filter(sPrincipal.type == ptype).one())
                    except NoResultFound:
                        sprincipal = sPrincipal()
                        sprincipal.name = pname
                        sprincipal.type = ptype
                        self.session.add(sprincipal)
                        self.session.flush()

                    spolicy.principal_name = sprincipal.name
                    spolicy.principal_type = sprincipal.type
                    self.session.merge(spolicy)

    def _store_recipe(self, recipe):
        srecipe = sRecipe(recipe.name, recipe.desc)
        self._store_policy(recipe.name, recipe.policy)
        srecipe.recipe_string = self._store_recipe_string(recipe)
        return srecipe

    def _store_recipe_string(self, recipe_list):
        string = u''
        string += u'\n'.join([u'%s?%s' % (unicode(bag),
            unicode(filter_string)) for bag, filter_string in recipe_list])
        return string

    def _store_roles(self, user):
        usersign = user.usersign
        for role in user.roles:
            srole = sRole()
            srole.user = usersign
            srole.name = role
            self.session.merge(srole)

    def _store_tags(self, tags):
        return self.serializer.serialization.tags_as(tags)

    def _store_tiddler(self, tiddler):
        if tiddler.revision:
            srevision = sRevision(tiddler.title, tiddler.bag, tiddler.revision)
        else:
            srevision = sRevision(tiddler.title, tiddler.bag, None)

        if binary_tiddler(tiddler):
            tiddler.text = unicode(b64encode(tiddler.text))

        srevision.type = tiddler.type
        srevision.modified = tiddler.modified
        srevision.modifier = tiddler.modifier
        srevision.text = tiddler.text
        srevision.tags = self._store_tags(tiddler.tags)
        self.session.add(srevision)
        self.session.flush()

        for field in tiddler.fields:
            if field.startswith('server.'):
                continue
            sfield = sField(field, tiddler.fields[field])
            sfield.revision_number = srevision.number
            srevision.fields.append(sfield)
            self.session.merge(sfield)

        return srevision

    def _store_user(self, user):
        suser = sUser()
        suser.usersign = user.usersign
        suser.password = user._password
        suser.note = user.note
        return suser
