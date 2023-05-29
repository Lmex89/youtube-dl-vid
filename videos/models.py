from django.db import models
import uuid

from django_utils.choices import Choices, Choice


class StatusCodec(Choices):
    success = Choice(value=1, label="Exitoso")
    pending = Choice(value=2, label="Pendiente")
    error = Choice(value=3, label="Error")


# Create your models here.
class BaseModel(models.Model):
    id = models.AutoField(primary_key=True, unique=True)
    created_at = models.DateTimeField(
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        auto_now=True,
    )
    visible = models.BooleanField(default=True)

    def soft_delete(self):
        """soft  delete a model instance"""
        self.visible = False
        self.save()

    class Meta:
        abstract = True
        ordering = ["-created_at"]


class Categorias(BaseModel):
    id = models.UUIDField(primary_key=True, unique=True, default=uuid.uuid4)
    title = models.CharField(max_length=250, null=False)

class CodecUrls(BaseModel):
    id = models.UUIDField(primary_key=True, unique=True, default=uuid.uuid4)
    url = models.CharField(max_length=350, null=False)
    status = models.PositiveSmallIntegerField(StatusCodec.choices, default=StatusCodec.pending)

class VideosUploaded(BaseModel):
    id = models.UUIDField(primary_key=True, unique=True, default=uuid.uuid4)
    video = models.FieldFile(upload_to="videos_up/portada", blank=True, null=True)
    title = models.CharField(max_length=250, null=False, default="test")
    category = models.ForeignKey(Categorias, null=True, on_delete=models.SET_NULL)
    codecurl = models.ForeignKey(CodecUrls, null=True, on_delete=models.SET_NULL)

