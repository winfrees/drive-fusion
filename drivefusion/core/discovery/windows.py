"""Windows volume and physical-drive discovery.

Identity comes from the volume GUID path and the device serial, never from a
drive letter — letters are reassigned between sessions and an archive drive
that comes back as ``F:`` instead of ``E:`` must still be the same drive
(docs/PLAN.md §5).

This module is on the raw-handle allowlist in ``tools/ro_lint.py``. Every
handle it opens requests **zero access rights**: ``IOCTL_STORAGE_QUERY_PROPERTY``
and the volume-extent query are documented to work that way, so the module
cannot read or write device contents even by mistake. The lint separately
rejects any write access right appearing here.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from drivefusion.core.discovery.base import DriveInfo, DiscoveryError, VolumeInfo

PROVIDER = "windows"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
OPEN_EXISTING = 3
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002

# GetVolumeInformationW capability flags.
FILE_READ_ONLY_VOLUME = 0x00080000
FILE_SUPPORTS_HARD_LINKS = 0x00400000
FILE_SUPPORTS_OPEN_BY_FILE_ID = 0x01000000
FILE_SUPPORTS_USN_JOURNAL = 0x02000000

IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
FSCTL_IS_VOLUME_DIRTY = 0x00090078

StorageDeviceProperty = 0
StorageDeviceSeekPenaltyProperty = 7
PropertyStandardQuery = 0

BUS_TYPES = {
    1: "scsi", 2: "atapi", 3: "ata", 4: "1394", 5: "ssa", 6: "fibre",
    7: "usb", 8: "raid", 9: "iscsi", 10: "sas", 11: "sata", 12: "sd",
    13: "mmc", 14: "virtual", 15: "file-backed-virtual", 17: "nvme",
}


class STORAGE_PROPERTY_QUERY(ctypes.Structure):
    _fields_ = [
        ("PropertyId", wintypes.DWORD),
        ("QueryType", wintypes.DWORD),
        ("AdditionalParameters", ctypes.c_byte * 1),
    ]


class STORAGE_DEVICE_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.DWORD),
        ("Size", wintypes.DWORD),
        ("DeviceType", ctypes.c_byte),
        ("DeviceTypeModifier", ctypes.c_byte),
        ("RemovableMedia", ctypes.c_byte),
        ("CommandQueueing", ctypes.c_byte),
        ("VendorIdOffset", wintypes.DWORD),
        ("ProductIdOffset", wintypes.DWORD),
        ("ProductRevisionOffset", wintypes.DWORD),
        ("SerialNumberOffset", wintypes.DWORD),
        ("BusType", wintypes.DWORD),
        ("RawPropertiesLength", wintypes.DWORD),
        ("RawDeviceProperties", ctypes.c_byte * 1),
    ]


class DEVICE_SEEK_PENALTY_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.DWORD),
        ("Size", wintypes.DWORD),
        ("IncursSeekPenalty", ctypes.c_byte),
    ]


class DISK_EXTENT(ctypes.Structure):
    _fields_ = [
        ("DiskNumber", wintypes.DWORD),
        ("StartingOffset", ctypes.c_longlong),
        ("ExtentLength", ctypes.c_longlong),
    ]


class VOLUME_DISK_EXTENTS(ctypes.Structure):
    _fields_ = [
        ("NumberOfDiskExtents", wintypes.DWORD),
        ("Extents", DISK_EXTENT * 8),
    ]


def _open_device(path: str) -> wintypes.HANDLE:
    """Open a device with **no** access rights, only the right to query it."""
    handle = kernel32.CreateFileW(
        path,
        0,  # zero desired access: query only, cannot read or write contents
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        raise DiscoveryError(
            f"cannot open {path}: WinError {ctypes.get_last_error()}"
        )
    return handle


def _device_ioctl(handle, code: int, buffer, in_buffer=None) -> bool:
    returned = wintypes.DWORD()
    in_ptr = ctypes.byref(in_buffer) if in_buffer is not None else None
    in_size = ctypes.sizeof(in_buffer) if in_buffer is not None else 0
    ok = kernel32.DeviceIoControl(
        handle,
        wintypes.DWORD(code),
        in_ptr,
        wintypes.DWORD(in_size),
        ctypes.byref(buffer),
        wintypes.DWORD(ctypes.sizeof(buffer)),
        ctypes.byref(returned),
        None,
    )
    return bool(ok)


def _ascii_at(raw: bytes, offset: int) -> str | None:
    if not offset or offset >= len(raw):
        return None
    end = raw.find(b"\x00", offset)
    value = raw[offset : end if end != -1 else len(raw)]
    return value.decode("ascii", "replace").strip() or None


def _describe_physical_drive(disk_number: int) -> DriveInfo:
    handle = _open_device(f"\\\\.\\PhysicalDrive{disk_number}")
    try:
        query = STORAGE_PROPERTY_QUERY(
            PropertyId=StorageDeviceProperty, QueryType=PropertyStandardQuery
        )
        buffer = ctypes.create_string_buffer(2048)
        if not _device_ioctl(
            handle, IOCTL_STORAGE_QUERY_PROPERTY, buffer, query
        ):
            return DriveInfo()

        descriptor = ctypes.cast(
            buffer, ctypes.POINTER(STORAGE_DEVICE_DESCRIPTOR)
        ).contents
        raw = buffer.raw
        serial = _ascii_at(raw, descriptor.SerialNumberOffset)
        vendor = _ascii_at(raw, descriptor.VendorIdOffset)
        product = _ascii_at(raw, descriptor.ProductIdOffset)

        # Seek penalty distinguishes spinning disks from solid state, which
        # sets read concurrency during hashing (docs/PLAN.md §6.6).
        media = "unknown"
        penalty_query = STORAGE_PROPERTY_QUERY(
            PropertyId=StorageDeviceSeekPenaltyProperty,
            QueryType=PropertyStandardQuery,
        )
        penalty = DEVICE_SEEK_PENALTY_DESCRIPTOR()
        if _device_ioctl(
            handle, IOCTL_STORAGE_QUERY_PROPERTY, penalty, penalty_query
        ):
            media = "hdd" if penalty.IncursSeekPenalty else "ssd"

        return DriveInfo(
            serial=serial,
            model=product,
            manufacturer=vendor,
            bus=BUS_TYPES.get(descriptor.BusType, "unknown"),
            media=media,
        )
    finally:
        kernel32.CloseHandle(handle)


def _disk_number_for_volume(volume_guid: str) -> int | None:
    # The device path form has no trailing separator.
    device_path = volume_guid.rstrip("\\")
    try:
        handle = _open_device(device_path)
    except DiscoveryError:
        return None
    try:
        extents = VOLUME_DISK_EXTENTS()
        if not _device_ioctl(
            handle, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, extents
        ):
            return None
        if extents.NumberOfDiskExtents < 1:
            return None
        return int(extents.Extents[0].DiskNumber)
    finally:
        kernel32.CloseHandle(handle)


def _is_dirty(volume_guid: str) -> bool | None:
    """exFAT has no journal, so an unclean dismount is a real signal."""
    try:
        handle = _open_device(volume_guid.rstrip("\\"))
    except DiscoveryError:
        return None
    try:
        flags = wintypes.DWORD()
        if not _device_ioctl(handle, FSCTL_IS_VOLUME_DIRTY, flags):
            return None
        return bool(flags.value & 0x01)
    finally:
        kernel32.CloseHandle(handle)


def _volume_guid_for_path(path: str) -> str:
    mount = ctypes.create_unicode_buffer(1024)
    if not kernel32.GetVolumePathNameW(path, mount, len(mount)):
        raise DiscoveryError(
            f"cannot resolve mount point for {path!r}: "
            f"WinError {ctypes.get_last_error()}"
        )
    guid = ctypes.create_unicode_buffer(1024)
    if not kernel32.GetVolumeNameForVolumeMountPointW(
        mount.value, guid, len(guid)
    ):
        raise DiscoveryError(
            f"cannot resolve volume GUID for {mount.value!r}: "
            f"WinError {ctypes.get_last_error()}"
        )
    return guid.value


def _volume_details(volume_guid: str) -> dict:
    label = ctypes.create_unicode_buffer(261)
    fs_name = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_component = wintypes.DWORD()
    flags = wintypes.DWORD()

    ok = kernel32.GetVolumeInformationW(
        volume_guid,
        label, len(label),
        ctypes.byref(serial),
        ctypes.byref(max_component),
        ctypes.byref(flags),
        fs_name, len(fs_name),
    )
    if not ok:
        return {}

    sectors_per_cluster = wintypes.DWORD()
    bytes_per_sector = wintypes.DWORD()
    free_clusters = wintypes.DWORD()
    total_clusters = wintypes.DWORD()
    cluster_bytes = None
    if kernel32.GetDiskFreeSpaceW(
        volume_guid,
        ctypes.byref(sectors_per_cluster),
        ctypes.byref(bytes_per_sector),
        ctypes.byref(free_clusters),
        ctypes.byref(total_clusters),
    ):
        cluster_bytes = sectors_per_cluster.value * bytes_per_sector.value

    free_bytes = ctypes.c_ulonglong()
    total_bytes = ctypes.c_ulonglong()
    total_free = ctypes.c_ulonglong()
    capacity = free = None
    if kernel32.GetDiskFreeSpaceExW(
        volume_guid,
        ctypes.byref(free_bytes),
        ctypes.byref(total_bytes),
        ctypes.byref(total_free),
    ):
        capacity = total_bytes.value
        free = total_free.value

    value = flags.value
    return {
        "label": label.value or None,
        "fs_type": fs_name.value or None,
        "cluster_bytes": cluster_bytes,
        "capacity_bytes": capacity,
        "free_bytes": free,
        "supports_hardlink": bool(value & FILE_SUPPORTS_HARD_LINKS),
        "supports_usn": bool(value & FILE_SUPPORTS_USN_JOURNAL),
        "supports_file_ids": bool(value & FILE_SUPPORTS_OPEN_BY_FILE_ID),
        "read_only": bool(value & FILE_READ_ONLY_VOLUME),
    }


def _drive_letter(volume_guid: str) -> str | None:
    length = wintypes.DWORD()
    buffer = ctypes.create_unicode_buffer(1024)
    if not kernel32.GetVolumePathNamesForVolumeNameW(
        volume_guid, buffer, len(buffer), ctypes.byref(length)
    ):
        return None
    names = [n for n in buffer[: length.value].split("\x00") if n]
    return names[0] if names else None


def _build(volume_guid: str) -> VolumeInfo:
    details = _volume_details(volume_guid)
    drive = None
    disk_number = _disk_number_for_volume(volume_guid)
    if disk_number is not None:
        try:
            drive = _describe_physical_drive(disk_number)
        except DiscoveryError:
            drive = None

    return VolumeInfo(
        volume_guid=volume_guid,
        label=details.get("label"),
        fs_type=details.get("fs_type"),
        capacity_bytes=details.get("capacity_bytes"),
        free_bytes=details.get("free_bytes"),
        cluster_bytes=details.get("cluster_bytes"),
        last_letter=_drive_letter(volume_guid),
        dirty_flag=_is_dirty(volume_guid),
        supports_hardlink=details.get("supports_hardlink"),
        supports_usn=details.get("supports_usn"),
        supports_file_ids=details.get("supports_file_ids"),
        drive=drive,
        provider=PROVIDER,
    )


def identify_volume(path: str) -> VolumeInfo:
    return _build(_volume_guid_for_path(os.path.abspath(path)))


def list_volumes() -> list[VolumeInfo]:
    buffer = ctypes.create_unicode_buffer(1024)
    handle = kernel32.FindFirstVolumeW(buffer, len(buffer))
    if handle == INVALID_HANDLE_VALUE:
        return []

    volumes: list[VolumeInfo] = []
    try:
        while True:
            try:
                volumes.append(_build(buffer.value))
            except DiscoveryError:
                pass  # a volume that vanished mid-enumeration is not an error
            if not kernel32.FindNextVolumeW(handle, buffer, len(buffer)):
                break
    finally:
        kernel32.FindVolumeClose(handle)
    return volumes
