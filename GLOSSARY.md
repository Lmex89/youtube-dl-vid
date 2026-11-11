# YouTube Downloader API

API REST que descarga videos de YouTube vía yt-dlp, soportando múltiples perfiles de salida (video estándar, video optimizado para WhatsApp, solo audio) y almacenando los artifacts resultantes.

## Language

### Entidades principales

**Solicitud de descarga (CodecUrl)**:
Intento de descarga de una URL de YouTube con un perfil de salida específico. Tiene estado (pending/success/error) y progreso (0-100%). No es la URL en sí, sino el *request* completo de descarga.
_Avoid_: Download, tarea de descarga

**Video subido (VideosUploaded)**:
Archivo multimedia descargado exitosamente y almacenado en disco. Representa el artifact final, no el proceso de descarga. Se crea solo cuando la solicitud de descarga completa con éxito.
_Avoid_: Download, archivo, media

**Categoría (Categorias)**:
Clasificación de primer nivel para organizar videos subidos. Relación uno-a-muchos: un video pertenece a una categoría, una categoría agrupa muchos videos.
_Avoid_: Género, tema, etiqueta

### Perfiles de salida

**Perfil de salida (Output Profile)**:
Configuración que define el formato, codec, resolución y restricciones de tamaño del archivo resultante. Determina cómo se descarga y post-procesa el video.
_Avoid_: Formato, preset, calidad

**Video estándar (Standard Video)**:
Perfil por defecto. MP4 con video hasta 720p y audio, sin re-encoding. Descarga directa de yt-dlp.
_Avoid_: Video normal, video original

**Video WhatsApp (WhatsApp Video)**:
Perfil que re-encodifica a MP4 H.264 Main/AAC para cumplir el límite de 16 MB de WhatsApp. Usa una escalera de resoluciones descendente (720p → 480p → 360p) hasta que el archivo quepa.
_Avoid_: Video comprimido, video liviano

**Audio M4A (Audio M4A)**:
Perfil de solo audio. Extrae la pista de audio en formato AAC dentro de un contenedor M4A a 128 Kbps.
_Avoid_: MP3, audio solo

### Conceptos operativos

**Escalera de altura (Height Ladder)**:
Secuencia de resoluciones descendentes (720p, 480p, 360p) que se prueban en orden hasta que el archivo resultante cumpla el límite de tamaño. Usada por el perfil de Video WhatsApp.
_Avoid_: Lista de calidades, pasos de compresión

**Progreso**:
Porcentaje de avance de una solicitud de descarga, de 0 a 100. Se actualiza en tiempo real durante la descarga y el post-procesamiento. Se lee con `get_job_state` (`videos/download.py`), junto con el Estado; el job se inicia con `start_download_async`.
_Avoid_: Avance, porcentaje

**Estado**:
Condición actual de una solicitud de descarga: pendiente (2), exitoso (1) o error (3).
_Avoid_: Estatus, condición

**Borrado lógico (Soft Delete)**:
Marca un registro como invisible en lugar de eliminarlo de la base de datos. Los registros con `visible=False` se excluyen de las consultas por defecto.
_Avoid_: Desactivar, ocultar
