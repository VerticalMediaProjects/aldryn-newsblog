# -*- coding: utf-8 -*-

from __future__ import unicode_literals

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured
from django.core.urlresolvers import reverse
from django.db import connection, models
from django.db.models.signals import post_save
from django.dispatch import receiver
try:
    from django.utils.encoding import force_unicode
except ImportError:
    from django.utils.encoding import force_text as force_unicode

from django.utils.encoding import python_2_unicode_compatible
from django.utils.text import slugify as default_slugify
from django.utils.timezone import now
from django.utils.translation import ugettext_lazy as _, ugettext, override

from aldryn_apphooks_config.fields import AppHookConfigField
from aldryn_categories.fields import CategoryManyToManyField
from aldryn_categories.models import Category
from aldryn_people.models import Person
from aldryn_reversion.core import version_controlled_content
from aldryn_translation_tools.models import TranslationHelperMixin

from cms.models.fields import PlaceholderField
from cms.models.pluginmodel import CMSPlugin
from cms.utils.i18n import get_current_language, get_redirect_on_fallback
from djangocms_text_ckeditor.fields import HTMLField
from filer.fields.image import FilerImageField
from parler.models import TranslatableModel, TranslatedFields
from sortedm2m.fields import SortedManyToManyField
from taggit.managers import TaggableManager
from taggit.models import Tag

from .cms_appconfig import NewsBlogConfig
from .managers import RelatedManager
from .utils import strip_tags, get_plugin_index_data, get_request

import django.core.validators

if settings.LANGUAGES:
    LANGUAGE_CODES = [language[0] for language in settings.LANGUAGES]
elif settings.LANGUAGE:
    LANGUAGE_CODES = [settings.LANGUAGE]
else:
    raise ImproperlyConfigured(
        'Neither LANGUAGES nor LANGUAGE was found in settings.')


# At startup time, SQL_NOW_FUNC will contain the database-appropriate SQL to
# obtain the CURRENT_TIMESTAMP.
SQL_NOW_FUNC = {
    'mssql': 'GetDate()', 'mysql': 'NOW()', 'postgresql': 'now()',
    'sqlite': 'CURRENT_TIMESTAMP', 'oracle': 'CURRENT_TIMESTAMP'
}[connection.vendor]

SQL_IS_TRUE = {
    'mssql': '== TRUE', 'mysql': '= 1', 'postgresql': 'IS TRUE',
    'sqlite': '== 1', 'oracle': 'IS TRUE'
}[connection.vendor]


@python_2_unicode_compatible
@version_controlled_content
class Article(TranslationHelperMixin, TranslatableModel):
    translations = TranslatedFields(
        title=models.CharField(_('title'), max_length=234),
        slug=models.SlugField(
            verbose_name=_('slug'),
            max_length=255,
            db_index=True,
            blank=True,
            help_text=_(
                'Used in the URL. If changed, the URL will change. '
                'Clear it to have it re-created automatically.'),
        ),
        lead_in=HTMLField(
            verbose_name=_('Optional lead-in'), default='',
            help_text=_('Will be displayed in lists, and at the start of the '
                        'detail page (in bold)'),
            blank=True,
        ),
        meta_title=models.CharField(
            max_length=255, verbose_name=_('meta title'),
            blank=True, default=''),
        meta_description=models.TextField(
            verbose_name=_('meta description'), blank=True, default=''),
        meta_keywords=models.TextField(
            verbose_name=_('meta keywords'), blank=True, default=''),
        meta={'unique_together': (('language_code', 'slug', ), )},

        search_data=models.TextField(blank=True, editable=False)
    )

    content = PlaceholderField('newsblog_article_content',
                               related_name='newsblog_article_content')
    author = models.ForeignKey(Person, null=True, blank=True,
                               verbose_name=_('author'))
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_('owner'))
    app_config = AppHookConfigField(NewsBlogConfig,
                                    verbose_name=_('app. config'))
    categories = CategoryManyToManyField('aldryn_categories.Category',
                                         verbose_name=_('categories'),
                                         blank=True)
    publishing_date = models.DateTimeField(_('publishing date'),
                                           default=now)
    is_published = models.BooleanField(_('is published'), default=True,
                                       db_index=True)
    is_featured = models.BooleanField(_('is featured'), default=False,
                                      db_index=True)
    featured_image = FilerImageField(null=True, blank=True)

    tags = TaggableManager(blank=True)

    related = SortedManyToManyField('self', verbose_name=_('related articles'),
                                    blank=True)

    objects = RelatedManager()

    class Meta:
        ordering = ['-publishing_date']

    @property
    def published(self):
        """
        Returns True only if the article (is_published == True) AND has a
        published_date that has passed.
        """
        return (self.is_published and self.publishing_date <= now())

    @property
    def future(self):
        """
        Returns True if the article is published but is scheduled for a
        future date/time.
        """
        return (self.is_published and self.publishing_date > now())

    def get_absolute_url(self, language=None):
        """Returns the url for this Article in the selected permalink format."""
        if not language:
            language = get_current_language()
        kwargs = {}
        permalink_type = self.app_config.permalink_type
        if 'y' in permalink_type:
            kwargs.update(year=self.publishing_date.year)
        if 'm' in permalink_type:
            kwargs.update(month="%02d" % self.publishing_date.month)
        if 'd' in permalink_type:
            kwargs.update(day="%02d" % self.publishing_date.day)
        if 'i' in permalink_type:
            kwargs.update(pk=self.pk)
        if 's' in permalink_type:
            slug, lang = self.known_translation_getter(
                'slug', default=None, language_code=language)
            if slug and lang:
                site_id = getattr(settings, 'SITE_ID', None)
                if get_redirect_on_fallback(language, site_id):
                    language = lang
                kwargs.update(slug=slug)

        if self.app_config and self.app_config.namespace:
            namespace = '{0}:'.format(self.app_config.namespace)
        else:
            namespace = ''

        with override(language):
            return reverse('{0}article-detail'.format(namespace), kwargs=kwargs)

    def slugify(self, source_text, i=None):
        slug = default_slugify(source_text)
        if i is not None:
            slug += "_%d" % i
        return slug

    def get_search_data(self, language=None, request=None):
        """
        Provides an index for use with Haystack, or, for populating
        Article.translations.search_data.
        """
        if not self.pk:
            return ''
        if language is None:
            language = get_current_language()
        if request is None:
            request = get_request(language=language)
        description = self.safe_translation_getter('lead_in', '')
        text_bits = [strip_tags(description)]
        for category in self.categories.all():
            text_bits.append(
                force_unicode(category.safe_translation_getter('name')))
        for tag in self.tags.all():
            text_bits.append(force_unicode(tag.name))
        if self.content:
            plugins = self.content.cmsplugin_set.filter(language=language)
            for base_plugin in plugins:
                plugin_text_content = ' '.join(
                    get_plugin_index_data(base_plugin, request))
                text_bits.append(plugin_text_content)
        return ' '.join(text_bits)

    def save(self, *args, **kwargs):
        # Update the search index
        self.search_data = self.get_search_data()

        # Ensure there is an owner.
        if self.app_config.create_authors and self.author is None:
            self.author = Person.objects.get_or_create(
                user=self.owner,
                defaults={
                    'name': u' '.join((self.owner.first_name,
                                       self.owner.last_name))
                })[0]

        # Start with a naïve approach, if none provided.
        if not self.slug:
            self.slug = force_unicode(default_slugify(self.title))

        # NOTE: It is very important that we never allow a blank slug to be
        # saved to the database. If we do, then subsequent attempts to create an
        # article will fail Django validation on the form, since we are asking
        # it to enforce uniqueness for (language, slug) pairs, and the slug in
        # the form starts out empty. When Django tries to validate, it finds
        # that the (language, slug) pair already exists, and we never even get
        # here.
        #
        # Since the slug is derived from the title of the article, if the slug
        # is still empty, then the title must be /effectively/ untitled.
        if not self.slug:
            self.slug = ugettext('untitled-article')

        # Ensure we aren't colliding with an existing slug *for this language*.
        if not Article.objects.translated(
                slug=self.slug).exclude(pk=self.pk).exists():
            return super(Article, self).save(*args, **kwargs)

        for lang in LANGUAGE_CODES:
            #
            # We'd much rather just do something like:
            # Article.objects.translated(lang,
            # slug__startswith=self.slug)
            # But sadly, this isn't supported by Parler/Django, see:
            # http://django-parler.readthedocs.org/en/latest/api/\
            #     parler.managers.html#the-translatablequeryset-class
            #
            slugs = []
            all_slugs = Article.objects.language(lang).exclude(
                pk=self.pk).values_list('translations__slug', flat=True)
            for slug in all_slugs:
                if slug and slug.startswith((self.title, self.slug)):
                    slugs.append(slug)
            i = 1
            while True:
                slug = self.slugify(self.title or self.slug, i)
                if slug not in slugs:
                    self.slug = slug
                    return super(Article, self).save(*args, **kwargs)
                i += 1

    def __str__(self):
        return self.safe_translation_getter('title', any_language=True)


class PluginEditModeMixin(object):
    def get_edit_mode(self, request):
        """
        Returns True only if an operator is logged-into the CMS and is in
        edit mode.
        """
        return (request.toolbar and request.toolbar.edit_mode)


class NewsBlogCMSPlugin(CMSPlugin):
    """AppHookConfig aware abstract CMSPlugin class for Aldryn Newsblog"""
    # avoid reverse relation name clashes by not adding a related_name
    # to the parent plugin
    cmsplugin_ptr = models.OneToOneField(
        CMSPlugin, related_name='+', parent_link=True)

    app_config = models.ForeignKey(NewsBlogConfig)

    class Meta:
        abstract = True

    def copy_relations(self, old_instance):
        self.app_config = old_instance.app_config


@python_2_unicode_compatible
class NewsBlogArchivePlugin(PluginEditModeMixin, NewsBlogCMSPlugin):
    # NOTE: the PluginEditModeMixin is eventually used in the cmsplugin, not
    # here in the model.
    def __str__(self):
        return _('%s archive') % (self.app_config.get_app_title(), )


class NewsBlogArticleSearchPlugin(NewsBlogCMSPlugin):
    max_articles = models.PositiveIntegerField(
        _('max articles'), default=10,
        validators=[django.core.validators.MinValueValidator(1)],
        help_text=_('The maximum number of found articles display.')
    )

    def __str__(self):
        return _('%s archive') % (self.app_config.get_app_title(), )


@python_2_unicode_compatible
class NewsBlogAuthorsPlugin(PluginEditModeMixin, NewsBlogCMSPlugin):
    def get_authors(self, request):
        """
        Returns a queryset of authors (people who have published an article),
        annotated by the number of articles (article_count) that are visible to
        the current user. If this user is anonymous, then this will be all
        articles that are published and whose publishing_date has passed. If the
        user is a logged-in cms operator, then it will be all articles.
        """

        # The basic subquery (for logged-in content managers in edit mode)
        subquery = """
            SELECT COUNT(*)
            FROM aldryn_newsblog_article
            WHERE
                aldryn_newsblog_article.author_id =
                    aldryn_people_person.id AND
                aldryn_newsblog_article.app_config_id = %d"""

        # For other users, limit subquery to published articles
        if not self.get_edit_mode(request):
            subquery += """ AND
                aldryn_newsblog_article.is_published %s AND
                aldryn_newsblog_article.publishing_date <= %s
            """ % (SQL_IS_TRUE, SQL_NOW_FUNC, )

        # Now, use this subquery in the construction of the main query.
        query = """
            SELECT (%s) as article_count, aldryn_people_person.*
            FROM aldryn_people_person
        """ % (subquery % (self.app_config.pk, ), )

        raw_authors = list(Person.objects.raw(query))
        authors = [author for author in raw_authors if author.article_count]
        return sorted(authors, key=lambda x: x.article_count, reverse=True)

    def __str__(self):
        return _('%s authors') % (self.app_config.get_app_title(), )


@python_2_unicode_compatible
class NewsBlogCategoriesPlugin(PluginEditModeMixin, NewsBlogCMSPlugin):
    def __str__(self):
        return _('%s categories') % (self.app_config.get_app_title(), )

    def get_categories(self, request):
        """
        Returns a list of categories, annotated by the number of articles
        (article_count) that are visible to the current user. If this user is
        anonymous, then this will be all articles that are published and whose
        publishing_date has passed. If the user is a logged-in cms operator,
        then it will be all articles.
        """

        subquery = """
            SELECT COUNT(*)
            FROM aldryn_newsblog_article, aldryn_newsblog_article_categories
            WHERE
                aldryn_newsblog_article_categories.category_id =
                    aldryn_categories_category.id AND
                aldryn_newsblog_article_categories.article_id =
                    aldryn_newsblog_article.id AND
                aldryn_newsblog_article.app_config_id = %d
        """ % (self.app_config.pk, )

        if not self.get_edit_mode(request):
            subquery += """ AND
                aldryn_newsblog_article.is_published %s AND
                aldryn_newsblog_article.publishing_date <= %s
            """ % (SQL_IS_TRUE, SQL_NOW_FUNC, )

        query = """
            SELECT (%s) as article_count, aldryn_categories_category.*
            FROM aldryn_categories_category
        """ % (subquery, )

        raw_categories = list(Category.objects.raw(query))
        categories = [
            category for category in raw_categories if category.article_count]
        return sorted(categories, key=lambda x: x.article_count, reverse=True)


@python_2_unicode_compatible
class NewsBlogFeaturedArticlesPlugin(PluginEditModeMixin, NewsBlogCMSPlugin):
    article_count = models.PositiveIntegerField(
        default=1,
        validators=[django.core.validators.MinValueValidator(1)],
        help_text=_('The maximum number of featured articles display.')
    )

    def get_articles(self, request):
        if not self.article_count:
            return Article.objects.none()
        queryset = Article.objects
        if not self.get_edit_mode(request):
            queryset = queryset.published()
        queryset = queryset.active_translations(get_current_language()).filter(
            app_config=self.app_config,
            is_featured=True,
        )
        return queryset[:self.article_count]

    def __str__(self):
        if not self.pk:
            return 'featured articles'
        prefix = self.app_config.get_app_title()
        if self.article_count == 1:
            title = _('featured article')
        else:
            title = _('featured articles: %(count)s') % {
                'count': self.article_count,
            }
        return '{0} {1}'.format(prefix, title)


@python_2_unicode_compatible
class NewsBlogLatestArticlesPlugin(PluginEditModeMixin, NewsBlogCMSPlugin):
    latest_articles = models.IntegerField(
        default=5,
        help_text=_('The maximum number of latest articles to display.')
    )

    def get_articles(self, request):
        """
        Returns a queryset of the latest N articles. N is the plugin setting:
        latest_articles.
        """
        queryset = Article.objects
        if not self.get_edit_mode(request):
            queryset = queryset.published()
        queryset = queryset.active_translations(get_current_language()).filter(
            app_config=self.app_config
        )
        return queryset[:self.latest_articles]

    def __str__(self):
        return _('%s latest articles: %s') % (
            self.app_config.get_app_title(), self.latest_articles, )


@python_2_unicode_compatible
class NewsBlogRelatedPlugin(PluginEditModeMixin, CMSPlugin):
    # NOTE: This one does NOT subclass NewsBlogCMSPlugin. This is because this
    # plugin can really only be placed on the article detail view in an apphook.
    cmsplugin_ptr = models.OneToOneField(
        CMSPlugin, related_name='+', parent_link=True)

    def get_articles(self, article, request):
        """
        Returns a queryset of articles that are related to the given article.
        """
        qs = article.related.all()
        if not self.get_edit_mode(request):
            qs = qs.published()
        return qs

    def __str__(self):
        return _('Related articles')


@python_2_unicode_compatible
class NewsBlogTagsPlugin(PluginEditModeMixin, NewsBlogCMSPlugin):

    def get_tags(self, request):
        """
        Returns a queryset of tags, annotated by the number of articles
        (article_count) that are visible to the current user. If this user is
        anonymous, then this will be all articles that are published and whose
        publishing_date has passed. If the user is a logged-in cms operator,
        then it will be all articles.
        """

        article_content_type = ContentType.objects.get_for_model(Article)

        subquery = """
            SELECT COUNT(*)
            FROM aldryn_newsblog_article, taggit_taggeditem
            WHERE
                taggit_taggeditem.tag_id = taggit_tag.id AND
                taggit_taggeditem.content_type_id = %d AND
                taggit_taggeditem.object_id = aldryn_newsblog_article.id AND
                aldryn_newsblog_article.app_config_id = %d"""

        if not self.get_edit_mode(request):
            subquery += """ AND
                aldryn_newsblog_article.is_published %s AND
                aldryn_newsblog_article.publishing_date <= %s
            """ % (SQL_IS_TRUE, SQL_NOW_FUNC, )

        query = """
            SELECT (%s) as article_count, taggit_tag.*
            FROM taggit_tag
        """ % (subquery % (article_content_type.id, self.app_config.pk), )

        raw_tags = list(Tag.objects.raw(query))
        tags = [tag for tag in raw_tags if tag.article_count]
        return sorted(tags, key=lambda x: x.article_count, reverse=True)

    def __str__(self):
        return _('%s tags') % (self.app_config.get_app_title(), )


@receiver(post_save)
def update_seach_index(sender, instance, **kwargs):
    """
    Upon detecting changes in a plugin used in an Article's content
    (PlaceholderField), update the article's search_index so that we can
    perform simple searches even without Haystack, etc.
    """
    if issubclass(instance.__class__, CMSPlugin):
        placeholder = getattr(instance, '_placeholder_cache', None) or instance.placeholder
        if hasattr(placeholder, '_attached_model_cache'):
            if placeholder._attached_model_cache == Article:
                article = placeholder._attached_model_cache.objects.get(
                    content=placeholder.pk)
                article.search_data = article.get_search_data(instance.language)
                article.save()
