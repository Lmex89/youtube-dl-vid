import uuid

from django.db import models


class StatusCodec(models.IntegerChoices):
    SUCCESS = 1, "Exitoso"
    PENDING = 2, "Pendiente"
    ERROR = 3, "Error"


class BaseModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    visible = models.BooleanField(default=True)

    def soft_delete(self):
        self.visible = False
        self.save()

    class Meta:
        abstract = True
        ordering = ["-created_at"]


class Categorias(BaseModel):
    id = models.UUIDField(primary_key=True, unique=True, default=uuid.uuid4)
    title = models.CharField(max_length=250)


class CodecUrls(BaseModel):
    id = models.UUIDField(primary_key=True, unique=True, default=uuid.uuid4)
    url = models.CharField(max_length=350)
    status = models.PositiveSmallIntegerField(
        choices=StatusCodec.choices, default=StatusCodec.PENDING
    )

    def __str__(self):
        return f"id={self.id}, url={self.url} status={self.status}"


class VideosUploaded(BaseModel):
    id = models.UUIDField(primary_key=True, unique=True, default=uuid.uuid4)
    video_path = models.CharField(max_length=500, blank=True, null=True)
    title = models.CharField(max_length=250, default="test")
    category = models.ForeignKey(Categorias, null=True, on_delete=models.SET_NULL)
    codecurl = models.ForeignKey(CodecUrls, null=True, on_delete=models.SET_NULL)

    def __str__(self):
        return f"id={self.id}, video_path={self.video_path} title={self.title}"
