"""Capture d'une caméra USB par Media Foundation (WinRT MediaCapture), agent 1.50.4.

Les paquets OpenCV de pip ne livrent pas leur module Media Foundation ; ici, c'est Windows lui-même qui capture
(même mécanisme que l'application Caméra ou Teams) : le Frame Server partage les caméras entre programmes et
négocie le format (MJPEG préféré : moins de bande passante USB, ce qui permet deux caméras sur un même contrôleur).
Même protocole que usb_camera_capture.py (STATUS / FRAME sur la sortie standard), lancé dans un processus par caméra.

Dépendances (profil « usb_camera » de l'installateur) : winrt-runtime et les espaces de noms Windows.Devices.Enumeration,
Windows.Media.Capture(.Frames), Windows.Media.MediaProperties, Windows.Graphics.Imaging, Windows.Storage.Streams.
"""
import asyncio
import json
import sys
import threading
import time

MAX_FRAME_BYTES = 1024 * 1024
# Formats de la caméra, du plus économe en bande passante USB au plus lourd.
SUBTYPE_PRIORITY = ("MJPG", "NV12", "YUY2")


class MediaFoundationUnavailable(RuntimeError):
    """WinRT absent (dépendances non installées) ou Media Foundation hors service."""


def choose_format(formats, width, height, fps):
    """Parmi (subtype, largeur, hauteur, cadence), le format demandé : taille exacte si réglée (sinon la plus
    grande ≤ 1280 de large), sous-type par SUBTYPE_PRIORITY, cadence la plus proche de ``fps``. None si rien."""
    candidates = [item for item in formats if str(item[0]).upper() in SUBTYPE_PRIORITY]
    if width and height:
        candidates = [item for item in candidates if item[1] == width and item[2] == height]
    else:
        # Sans réglage : 640×480 si la caméra le propose (léger, suffisant pour une surveillance), sinon la plus
        # grande taille ≤ 1280 de large.
        standard = [item for item in candidates if (item[1], item[2]) == (640, 480)]
        candidates = standard or [item for item in candidates if item[1] <= 1280]
    if not candidates:
        return None
    largest = max(item[1] * item[2] for item in candidates)
    candidates = [item for item in candidates if item[1] * item[2] == largest] if not (width and height) else candidates
    return min(candidates, key=lambda item: (SUBTYPE_PRIORITY.index(str(item[0]).upper()), abs(item[3] - fps), -item[3]))


def emit(line):
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()


QUIET = False          # automatique : un échec n'est pas annoncé, l'appelant retombe sur DirectShow
LAST_FAILURE = {}      # dernier échec retenu quand QUIET est vrai


def status(**facts):
    if QUIET and not facts.get("opened"):
        LAST_FAILURE.clear()
        LAST_FAILURE.update(facts)
        return
    emit(b"STATUS " + json.dumps(facts, ensure_ascii=True).encode("ascii") + b"\n")


async def list_cameras():
    from winrt.windows.devices.enumeration import DeviceClass, DeviceInformation
    devices = await DeviceInformation.find_all_async_device_class(DeviceClass.VIDEO_CAPTURE)
    return [(str(device.id), str(device.name)) for device in devices]


async def capture_loop(options, stop):
    """Ouvre la caméra ``options['index']`` et émet des JPEG à ``options['fps']`` jusqu'à ``stop``."""
    try:
        from winrt.windows.media.capture import (MediaCapture, MediaCaptureInitializationSettings, MediaCaptureMemoryPreference,
                                                 MediaCaptureSharingMode, StreamingCaptureMode)
        from winrt.windows.media.capture.frames import MediaFrameSourceKind
        from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapEncoder, BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataReader, InMemoryRandomAccessStream
    except ImportError as error:
        raise MediaFoundationUnavailable("Media Foundation : dépendances WinRT absentes (clique Installer la capture USB).") from error
    cameras = await list_cameras()
    names = [name for _, name in cameras]
    if options["index"] >= len(cameras):
        status(opened=False, error=f"Media Foundation ne voit que {len(cameras)} caméra(s)", cameras=names)
        return 2
    device_id, device_name = cameras[options["index"]]
    capture = MediaCapture()
    settings = MediaCaptureInitializationSettings()
    settings.video_device_id = device_id
    settings.streaming_capture_mode = StreamingCaptureMode.VIDEO
    settings.memory_preference = MediaCaptureMemoryPreference.CPU
    settings.sharing_mode = MediaCaptureSharingMode.EXCLUSIVE_CONTROL
    try:
        await capture.initialize_with_settings_async(settings)
    except Exception as error:  # noqa: BLE001 - la caméra est tenue par un autre programme, ou refusée par Windows
        status(opened=False, error=f"Media Foundation n’ouvre pas « {device_name} » ({type(error).__name__})", cameras=names)
        return 2
    source = next((item for item in capture.frame_sources.values() if item.info.source_kind == MediaFrameSourceKind.COLOR), None)
    if source is None:
        status(opened=False, error=f"« {device_name} » n’a pas de flux vidéo couleur", cameras=names)
        capture.close()
        return 2
    formats = []
    for fmt in source.supported_formats:
        rate = fmt.frame_rate.numerator / max(1, fmt.frame_rate.denominator)
        formats.append((str(fmt.subtype), int(fmt.video_format.width), int(fmt.video_format.height), float(rate), fmt))
    chosen = choose_format([item[:4] for item in formats], options["width"], options["height"], options["fps"])
    if chosen is None:
        status(opened=False, error=f"« {device_name} » ne propose pas {options['width']}×{options['height']} (formats : "
               + ", ".join(sorted({f'{w}×{h}' for _, w, h, _ in (item[:4] for item in formats)})) + ")", cameras=names)
        capture.close()
        return 2
    native = next(item[4] for item in formats if item[:4] == chosen)
    try:
        await source.set_format_async(native)
    except Exception:  # noqa: BLE001 - format refusé : la caméra garde le sien
        pass
    subtype = str(chosen[0]).upper()
    # MJPEG : lecteur natif (Windows décode) ; sinon conversion BGRA8 par le lecteur.
    reader = await (capture.create_frame_reader_async(source) if subtype == "MJPG"
                    else capture.create_frame_reader_with_subtype_async(source, "BGRA8"))
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=2)
    interval = 1.0 / options["fps"]
    last = [0.0]
    counts = {"arrived": 0, "bitmap": 0, "jpeg": 0, "empty": 0}

    def on_frame(sender, args):
        now = time.monotonic()
        reference = sender.try_acquire_latest_frame()
        if reference is None:
            return
        try:
            counts["arrived"] += 1
            if now - last[0] < interval * 0.9:
                return
            video = reference.video_media_frame
            bitmap = video.software_bitmap if video is not None else None
            if bitmap is not None:
                counts["bitmap"] += 1
                converted = SoftwareBitmap.convert_with_alpha(bitmap, BitmapPixelFormat.BGRA8, BitmapAlphaMode.IGNORE)
                last[0] = now
                loop.call_soon_threadsafe(_offer, queue, converted)
                return
            # MJPEG livré compressé, sans image décodée (selon la caméra) : c'est déjà un JPEG, il part tel quel.
            buffered = reference.buffer_media_frame
            data = _buffer_bytes(buffered.buffer) if buffered is not None else b""
            if data[:2] == b"\xff\xd8":
                counts["jpeg"] += 1
                last[0] = now
                loop.call_soon_threadsafe(_offer, queue, data)
            else:
                counts["empty"] += 1
        finally:
            reference.close()

    token = reader.add_frame_arrived(on_frame)
    started = await reader.start_async()
    if int(started) != 0:
        status(opened=False, error=f"Media Foundation refuse de démarrer « {device_name} » (code {int(started)})", cameras=names)
        reader.remove_frame_arrived(token)
        capture.close()
        return 2
    status(opened=True, backend=f"Media Foundation · {device_name} · {subtype} {chosen[1]}×{chosen[2]}", camera=device_name, cameras=names)
    code = 0
    try:
        misses = 0
        while not stop.is_set():
            try:
                bitmap = await asyncio.wait_for(queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                misses += 1
                if misses >= 5:
                    # Pas d'image en 10 s : caméra tenue par un autre programme, ou index qui ne désigne pas la bonne
                    # caméra (l'ordre Media Foundation diffère de celui de DirectShow). En automatique, l'appelant
                    # retombe sur DirectShow ; en « Media Foundation » forcé, la fiche affiche l'ordre des caméras.
                    status(opened=False, no_frames=True, cameras=names,
                           error=f"« {device_name} » (Media Foundation, index {options['index']}, {subtype} {chosen[1]}×{chosen[2]}) "
                                 f"ne fournit pas d’images (reçues : {counts['arrived']}, décodées : {counts['bitmap']}, "
                                 f"JPEG : {counts['jpeg']}, vides : {counts['empty']}) ; caméras vues par Media Foundation : "
                                 + ", ".join(f"{i} = {n}" for i, n in enumerate(names)))
                    code = 2
                    break
                continue
            misses = 0
            if isinstance(bitmap, bytes):
                if len(bitmap) < MAX_FRAME_BYTES:
                    emit(b"FRAME %d 0\n" % len(bitmap))
                    emit(bitmap)
                continue
            stream = InMemoryRandomAccessStream()
            encoder = await BitmapEncoder.create_async(BitmapEncoder.jpeg_encoder_id, stream)
            encoder.set_software_bitmap(bitmap)
            await encoder.flush_async()
            size = int(stream.size)
            bitmap.close()
            if size <= 0 or size >= MAX_FRAME_BYTES:
                stream.close()
                continue
            data_reader = DataReader(stream.get_input_stream_at(0))
            await data_reader.load_async(size)
            frame = bytes(data_reader.read_buffer(size))
            data_reader.close()
            stream.close()
            emit(b"FRAME %d 80\n" % len(frame))
            emit(frame)
    finally:
        try:
            reader.remove_frame_arrived(token)
            await reader.stop_async()
            reader.close()
            capture.close()
        except Exception:
            pass
    return code


def _buffer_bytes(buffer):
    """Octets d'un IBuffer WinRT (vue mémoire du runtime), copiés."""
    try:
        return bytes(memoryview(buffer))
    except TypeError:
        from winrt.windows.storage.streams import DataReader
        reader = DataReader.from_buffer(buffer)
        data = bytes(reader.read_buffer(int(buffer.length)))
        reader.close()
        return data


def _close(item):
    if not isinstance(item, bytes):
        try:
            item.close()
        except Exception:
            pass


def _offer(queue, item):
    if queue.full():
        try:
            _close(queue.get_nowait())
        except Exception:
            pass
    try:
        queue.put_nowait(item)
    except Exception:
        _close(item)


def run(options):
    """Point d'entrée pour usb_camera_capture.py : rend le code de sortie (0 arrêt demandé, 2 échec, 3 dépendances)."""
    stop = threading.Event()
    try:
        return asyncio.run(capture_loop(options, stop))
    except MediaFoundationUnavailable as error:
        status(opened=False, error=str(error), dependency=True)
        return 3
    except BrokenPipeError:
        return 0
