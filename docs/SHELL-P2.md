# M8 P2: shell namespace, undo, thumbnails

## Shell namespace

The Files page now has a **Компьютер** button. It opens `shell://` with:

- the workspace;
- Desktop;
- Documents;
- Downloads;
- Pictures;
- Music;
- Videos.

This is a metadata namespace: names, types and sizes only. It deliberately does
not enumerate drive roots or system directories. Writes and file reads outside
the workspace still go through `ProtectedZones`.

## Undo

The **Отменить** button and `Ctrl+Z` undo the last operation kept in
`data/fs-undo.json`:

| Operation | Undo |
|---|---|
| `fs.mkdir` | move the empty directory to the Recycle Bin |
| `fs.rename` | rename back |
| `fs.copy` | move the copy to the Recycle Bin |
| `fs.move` | move back |

`fs.trash` is not undoable by Kivi because Windows owns Recycle Bin restore.

## Thumbnails

`GET /fs/thumb?path=...` returns a 320×220 JPEG:

- images via Pillow;
- the first frame of MP4/WebM/MKV/MOV via OpenCV.

The file list loads thumbnails lazily only for recognized image files.

## Checks

```powershell
.venv\Scripts\python.exe -m pytest -q
kivi tools list
```

Manual API checks:

```text
GET /fs/list?path=shell:// → known folders
GET /fs/thumb?path=data/bench/ocr.png → image/jpeg
POST /fs/mkdir + POST /fs/undo → directory removed
```
