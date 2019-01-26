import re

from django.db import models as m, transaction
from django.utils.translation import ugettext as _
from wechatpy.exceptions import WeChatClientException

from .. import utils
from . import WeChatApp

class Material(m.Model):
    class Type(object):
        IMAGE = "image"
        VIDEO = "video"
        NEWS = "news"
        VOICE = "voice"

    app = m.ForeignKey(WeChatApp, on_delete=m.CASCADE,
        related_name="materials")
    type = m.CharField(_("type"), max_length=5,
        choices=(utils.enum2choices(Type)))
    media_id = m.CharField(_("media_id"), max_length=64)
    name = m.CharField(_("filename"), max_length=64, blank=True, null=True)
    url = m.CharField(_("url"), max_length=512, editable=False, null=True)
    update_time = m.IntegerField(_("update time"), editable=False, 
        null=True)

    comment = m.TextField(_("comment"), blank=True)
    
    created = m.DateTimeField(_("created"), auto_now_add=True)
    updated = m.DateTimeField(_("updated"), auto_now=True)

    class Meta(object):
        unique_together = (("app", "media_id"),)
        ordering = ("app", "-update_time")

    @classmethod
    def get_by_media(cls, app, media_id):
        return cls.objects.get(app=app, media_id=media_id)

    @classmethod
    def sync(cls, app, id=None, type=None):
        if id:
            data = app.client.material.get(id)
            return cls.create(app=app, type=type, media_id=id, **data)
        else:
            updated = []
            for type, _ in utils.enum2choices(cls.Type):
                with transaction.atomic():
                    updates = cls.sync_type(type, app)
                    updated.extend(updates)
            return updated

    @classmethod
    def sync_type(cls, type, app):
        count = 20
        offset = 0
        updates = []
        while True:
            data = app.client.material.batchget(
                media_type=type,
                offset=offset,
                count=count
            )
            updates.extend(data["item"])
            if data["total_count"] <= offset + count:
                break
            offset += count
        # 删除被删除的 更新或新增获取的
        (cls.objects.filter(app=app, type=type)
            .exclude(media_id__in=map(lambda o: o["media_id"], updates))
            .delete())
        return [cls.create(app=app, type=type, **item) for item in updates]

    @classmethod
    def as_permenant(cls, media_id, app, save=True):
        resp = app.client.media.download(media_id)
        
        try:
            content_type = resp.headers["Content-Type"]
        except:
            raise ValueError("missing Content-Type")
        if content_type.startswith("image"):
            type = cls.Type.IMAGE
        elif content_type.startswith("video"):
            type = cls.Type.VIDEO
        elif content_type.startswith("audio"):
            type = cls.Type.VOICE
        else:
            raise ValueError("unknown Content-Type")

        try:    
            disposition = resp.headers["Content-Disposition"]
            filename = re.findall(r'filename="(.+?)"', disposition)[0]
        except:
            # TODO: 默认文件名
            filename = None
        
        return cls.upload_permenant((filename, resp.content), type, app, save)

    @classmethod
    def upload_permenant(cls, file, type, app, save=True):
        # 上传文件
        data = app.client.material.add(type, file)
        media_id = data["media_id"]
        if save:
            rv = cls(type=type, media_id=media_id, url=data.get("url"))
            return rv.save()
        else:
            return media_id

    @classmethod
    def create(cls, app, type=None, **kwargs):
        from . import Article
        # TODO: type为None的情况
        if type is None:
            pass
        if type == cls.Type.NEWS:
            # 移除所有article重新插入
            query = dict(app=app, media_id=kwargs["media_id"])
            record = dict(type=type, update_time=kwargs["update_time"])
            record.update(query)
            news, created = cls.objects.update_or_create(record, **query)
            if not created:
                news.articles.all().delete()
            articles = (kwargs.get("content") or kwargs)["news_item"]
            # 同步thumb_media_id 日
            for article in articles:
                if not "thumb_url" in article:
                    thumb_media_id = article.get("thumb_media_id")
                    if thumb_media_id:
                        image = cls.objects.filter(
                            app=app, media_id=thumb_media_id).first()
                        if not image:
                            try:
                                image = cls.sync(app, thumb_media_id, cls.Type.IMAGE)
                                article["thumb_url"] = image.url
                            except WeChatClientException as e:
                                # 可能存在封面不存在的情况
                                if e.errcode != 400007:
                                    raise
            
            Article.objects.bulk_create([
                Article(index=idx, material=news, **article) # TODO: 过滤article fields
                for idx, article in enumerate(articles)
            ])
            return news
        else:
            allowed_keys = map(lambda o: o.name, cls._meta.fields)
            kwargs = {key: kwargs[key] for key in allowed_keys if key in kwargs}
            record = dict(app=app, type=type, **kwargs)
            return cls.objects.update_or_create(record, **record)[0]

    def delete(self, *args, **kwargs):
        rv = super().delete(*args, **kwargs)
        self.app.client.material.delete(self.media_id)
        return rv

    def __str__(self):
        return "{type}:{media_id}".format(type=self.type, media_id=self.media_id)