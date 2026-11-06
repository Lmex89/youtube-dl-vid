# Las solicitudes de descarga reemplazadas se eliminan con sus artifacts

Cuando una Solicitud de descarga (`CodecUrls`) termina exitosamente, reemplaza a las
solicitudes anteriores con la misma URL y perfil de salida: sus archivos finales,
directorios de staging, logs y Videos subidos (`VideosUploaded`) se eliminan.

Se decidió eliminar también las filas de `CodecUrls` reemplazadas **solo cuando su
estado es `SUCCESS`** (`StatusCodec.SUCCESS`). Los intentos fallidos (`ERROR`) se
conservan como historial, tal como establece el ADR 0001.

La razón es que un artifact exitoso tiene un único dueño: si la fila de `CodecUrls`
sobreviviera sin su archivo, la API expondría una solicitud "exitosa" que ya no puede
servir nada y los clientes que guarden ese id obtendrían un estado incoherente. En
cambio, un intento fallido nunca tuvo artifact, así que conservarlo como historial es
útil para diagnóstico y no promete nada que no exista.

No se agregó un campo marcador ni una migración: `status` ya distingue un artifact
exitoso de un intento fallido, y es la única condición necesaria para decidir la
eliminación. Los jobs en curso no se consideran reemplazados, por lo que nunca se
borra una solicitud que todavía está descargando.

Consecuencias:

- Tras un reemplazo, los ids de solicitudes exitosas anteriores dejan de existir y sus
  endpoints responden 404.
- Los intentos fallidos permanecen consultables como historial.
- El conteo `codecurls_deleted` de los eventos de ciclo de vida refleja únicamente las
  filas realmente eliminadas.
- La limpieza de huérfanos solo borra archivos que estén bajo el directorio de
  descargas; nunca toca rutas arbitrarias.
