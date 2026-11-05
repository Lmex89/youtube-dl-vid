# Solicitud de descarga y artifact subido son entidades separadas

El modelo de dominio separa `CodecUrls` (solicitud de descarga con URL, perfil, estado y progreso) de `VideosUploaded` (artifact final almacenado en disco). `VideosUploaded` referencia `CodecUrls` vía FK nullable.

Se eligió esta separación porque una solicitud de descarga puede fallar, estar en progreso o completarse exitosamente, pero el artifact solo existe cuando la descarga termina bien. Esto permite:

- Historial de intentos de descarga (incluidos los fallidos) independiente de los artifacts exitosos
- Tracking de progreso en tiempo real sin mezclarlo con datos del archivo final
- Descargas que fallan dejan rastro en `CodecUrls` sin crear registros incompletos en `VideosUploaded`
- Múltiples solicitudes para la misma URL con diferentes perfiles de salida

El costo es mayor complejidad en la API (dos conjuntos de endpoints) y en el flujo de descarga (crear `VideosUploaded` solo al final, con FK nullable que permite cleanup si la descarga falla).
