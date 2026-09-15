import uuid

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class StatusCodec(models.IntegerChoices):
    SUCCESS = 1, "Exitoso"
    PENDING = 2, "Pendiente"
    ERROR = 3, "Error"


class OutputProfile(models.TextChoices):
    STANDARD_VIDEO = "standard_video", "Video estandar"
    WHATSAPP_VIDEO = "whatsapp_video", "Video WhatsApp"
    AUDIO_M4A = "audio_m4a", "Audio M4A"


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
    output_profile = models.CharField(
        max_length=32,
        choices=OutputProfile.choices,
        default=OutputProfile.STANDARD_VIDEO,
    )
    status = models.PositiveSmallIntegerField(
        choices=StatusCodec.choices, default=StatusCodec.PENDING
    )
    progress = models.FloatField(
        default=0.0,
        validators=[MinValueValidator(0.0), MaxValueValidator(100.0)],
    )

    def __str__(self):
        return (
            f"id={self.id}, url={self.url} profile={self.output_profile} "
            f"status={self.status}"
        )


class VideosUploaded(BaseModel):
    id = models.UUIDField(primary_key=True, unique=True, default=uuid.uuid4)
    file_path = models.CharField(max_length=500, blank=True, null=True)
    mime_type = models.CharField(max_length=100, blank=True, default="")
    title = models.CharField(max_length=250, default="test")
    category = models.ForeignKey(Categorias, null=True, on_delete=models.SET_NULL)
    codecurl = models.ForeignKey(CodecUrls, null=True, on_delete=models.SET_NULL)

    def __str__(self):
        return f"id={self.id}, file_path={self.file_path} title={self.title}"
