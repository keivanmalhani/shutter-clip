# shutter-clip

[![CI](https://github.com/keivanmalhani/shutter-clip/actions/workflows/ci.yml/badge.svg)](https://github.com/keivanmalhani/shutter-clip/actions/workflows/ci.yml)
![Licencia MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Python 3.11+ solo stdlib](https://img.shields.io/badge/python-3.11%2B%20solo%20stdlib-blue.svg)

[English](README.md) | Espanol

![Demo de shutter-clip: escanea un disco de material, publica picks rankeados por movimiento, rankea a fondo con shutter-select](docs/demo.gif)

Clips sociales sin editar, directo del disco de material. Apuntalo al SSD y obten archivos listos para el telefono que puedes mandar por AirDrop y postear tal cual. Nada se sube a ningun lado y los archivos fuente nunca se tocan.

Parte de la familia shutter- junto a shutter-cull, shutter-select y shutter-mcp. Fase 0 del motor de clips sociales.

## Requisitos

- ffmpeg y ffprobe en el PATH. macOS: `brew install ffmpeg`
- python3, solo libreria estandar, sin pip

## Inicio rapido

```zsh
python3 shutter_clip.py scan "/Volumes/Crucial X10"
```

```zsh
python3 shutter_clip.py mirror "/Volumes/Crucial X10"
```

Las salidas aterrizan en `_phone-ready/` en el disco, espejando la estructura de carpetas. Las carpetas cuyo nombre contiene "do not include" o "do not use" siempre se saltan, mas lo que pases con `--exclude`.

## Subcomandos

| comando | que hace |
| --- | --- |
| `scan` | inventario: codec, resolucion, fps, duracion, banderas HDR / 10-bit / perfil plano / sin audio |
| `mirror` | copia 1080p lista para telefono de cada clip en `_phone-ready/library/`. Incremental |
| `publish` | los mejores momentos rankeados por movimiento, nombres legibles, organizados en paquetes por plataforma y tipo de contenido |
| `rank` | ranking profundo usando el analisis de shutter-select: voz, calidad de audio, nitidez, movimiento, exposicion |
| `clips` | corte automatico simple en piezas de 6-18 s en los cambios de escena |
| `sheet` | hoja de contactos HTML autocontenida. Clic en cuadros para armar una lista de picks |
| `cut` | exporta los momentos exactos de un archivo de picks |
| `frames` | tiras de revision de 3 cuadros por cada pick publicado, numeradas para veredictos rapidos |
| `curate` | aplica veredictos `0012 kill` / `0007 top 3`: los lados B fuera, los mejores al frente |

## publish

Una sola pasada de analisis por video construye un perfil de movimiento. Las ventanas candidatas se rankean por energia de movimiento, con castigo cerca del primer y ultimo 8 por ciento del archivo, despegues y aterrizajes de drone, y en tramos oscuros, y nunca cruzan un corte duro. Los mejores 1 a 4 momentos por video, segun su duracion, se exportan en horizontal mas un gemelo vertical 9:16:

```text
_phone-ready/post-ready/
  tiktok + reels/
    drone aerials/brazil drone footage - pick 1 of 2 - 14s - DJI_0596 at 1m00s - horizontal.mp4
  shorts + stories - vertical/
    ...los mismos momentos en recorte vertical
```

El tipo de contenido sale de patrones del nombre de archivo, DJI es drone, DSCF y C0xxx son camara, IMG es telefono, con la carpeta como respaldo.

## rank

`publish` ve movimiento y brillo. `rank` ve todo lo que [shutter-select](https://github.com/keivanmalhani/shutter-select) puede medir: tomas con voz y lo que se dijo, calidad de audio, nitidez, exposicion, movimiento, presencia de rostros opcional. shutter-clip sigue siendo solo stdlib: nunca importa shutter-select, lee el JSON de analisis por archivo, esquema 1, que `shutter-select analyze` guarda bajo `_selects/cache/`, y con `--analyze` corre ese CLI como subproceso cuando el cache falta o esta viejo.

```zsh
python3 shutter_clip.py rank "/Volumes/Crucial X10" --analyze
```

Los segmentos se recalifican con pesos sociales, el movimiento y la energia de gancho valen mas que el pulido de entrevista, se rankean por percentil dentro de su clase en toda la corrida, y se recortan a duraciones posteables por tipo de contenido, drone 20 s, camara 12 s, telefono 8 s, `--clip-len` lo cambia. La voz distorsionada o inaudible y los recortes menores a `--min-len` se excluyen con razones en lenguaje claro. Todo aterriza en `_phone-ready/rank/`:

- `picks.txt`: los mejores momentos primero, listo para el comando cut
- `report.txt`: el ranking completo mas cada exclusion y su razon
- `ranking.json`: puntajes, features y transcripciones de cada segmento, la entrada para las fases de subtitulos y calendario

Si shutter-select no esta instalado, `rank` lo dice y imprime el comando exacto de analisis; nada se descarga ni importa en silencio.

## Formato de salida

- Horizontal 1920x1080 por defecto. `--vertical` agrega un recorte central 1080x1920, `--vertical-only` lo reemplaza.
- HEVC con etiqueta `hvc1`: la mitad del peso de H.264, nativo en iPhone. El encoder se elige solo: VideoToolbox por hardware en macOS, unas 5-10x el tiempo real, libx265 o libx264 en otros sistemas. Cambia con `--encoder x264` si algun reproductor se queja.
- Las fuentes HDR HLG y PQ se mapean a SDR bt709 automaticamente.
- Material en log: pasa `--lut tulook.cube` para hornear un look.

## Elegir momentos exactos

```zsh
python3 shutter_clip.py sheet "/Volumes/Crucial X10"
```

Abre `_phone-ready/contact-sheet.html`, haz clic en los cuadros que te gusten, copia los picks, guardalos como `picks.txt`, y despues:

```zsh
python3 shutter_clip.py cut picks.txt "/Volumes/Crucial X10"
```

Las lineas de picks se ven asi: `carpeta/clip.MOV @ 1:42` para 12 s desde ahi, `... @ 1:42-1:55` para un rango exacto, ` v` al final para vertical.

## Desarrollo

```zsh
python3 -m pytest tests/ -q
```

53 pruebas, sin media en el repo: los fixtures se generan con ffmpeg al momento. La suite cubre el ranking de movimiento, los nombres en lenguaje claro, los buckets de contenido, el corte en segmentos, toda la etapa rank con sus percentiles de empate promediado y el contrato de cache de shutter-select, y viajes redondos reales de ffmpeg para scan, cut y clips --copy.

## Notas

- `clips --copy` copia el stream original 4K sin perdida y casi sin tiempo, pero los cortes caen en keyframes, asi que los inicios pueden correrse un segundo o dos. El modo por defecto recodifica y es exacto al cuadro.
- `clips` decodifica cada archivo para detectar escenas. En un disco grande espera minutos en Apple Silicon, mucho mas sin decodificacion por hardware.
- `scan` muestrea un cuadro por archivo para adivinar el perfil plano. `--fast` lo salta.

## Licencia

MIT, ver [LICENSE](LICENSE).
