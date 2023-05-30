from django.contrib import admin

from videos.models import Categorias, CodecUrls, VideosUploaded


class CategoriasAdmin(admin.ModelAdmin):
    pass


# Register your models here.


class CodecUrlsAdmin(admin.ModelAdmin):
    pass


class VideosUploadedAdmin(admin.ModelAdmin):
    pass


admin.site.register(VideosUploaded, VideosUploadedAdmin)
admin.site.register(CodecUrls, CodecUrlsAdmin)
admin.site.register(Categorias, CategoriasAdmin)