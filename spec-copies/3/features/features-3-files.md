# Features.3: files

Routes covered: `POST /api/files`, `GET /api/files/{id}`, `GET /api/files/{id}/download`, and
`file_ids` on `POST /api/channels/{id}/messages`.
Check: `bash /spec/features/checks/check_features_3.sh`. The rule numbers below match its output.

Do not start this spec until `check_features_2.sh` prints `RESULT: 10 passed, 0 failed`.

The conventions in `features-1-profiles-dms.md` apply to every rule.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | Used here for |
|---|---|
| `user_from_token` | Who uploads and who downloads |
| `post_message`, `list_messages` | `post_message` now accepts `file_ids`; every message carries its `files` |

## Adds

| Function | What it does |
|---|---|
| `save_upload(user, filename, content_type, data)` | Writes the bytes to shared storage (on disk under `/app/data`, not in process memory) and stores the `FileObj` |
| `get_file(file_id)` | The `FileObj` or `None` |
| `attach_files(message, user, file_ids)` | Checks every id (exists, uploaded by `user`, not attached yet) before the message is stored, then links them |

## Rules (24-31)

`FileObj = {id, uploader_id, filename, content_type, size, created_at}`. `size` is the number of
bytes. `content_type` is the `Content-Type` of the uploaded part.

24. **Upload.** `POST /api/files` with `multipart/form-data` and one part named `file` returns
    `201` and `{file: FileObj}`. `filename` is the part's filename and `uploader_id` the caller.

25. **Metadata.** `GET /api/files/{id}` returns `200` and `{file}` on any node. An unknown id
    returns `404`. Uploading without a token returns `401`.

26. **Download.** `GET /api/files/{id}/download` (any node) returns `200` with the exact bytes
    uploaded, a `Content-Type` header equal to the file's `content_type`, and a
    `Content-Disposition` header that contains the filename, for example
    `attachment; filename="hello.txt"`.

27. **Binary safe.** Binary data (for example a PNG with zero bytes in it) comes back byte for
    byte. Never decode the upload as text.

28. **Size limit.** A file of exactly 10 MiB (10,485,760 bytes) is accepted. One byte more returns
    `413`. Answer the `413` with a normal HTTP response (read or drain the body first; do not just
    close the connection). Raise any framework body-size limit above 10 MiB so it does not reject
    first.

29. **Missing part.** A multipart body with no part named `file` returns `400`.

30. **Attach.** `POST /api/channels/{id}/messages` with `{body, file_ids: [id]}` returns `201`, and
    `message.files` is a list with that `FileObj`. The listing shows the same `files`. A message
    without files has `files: []`.

31. **Attach errors.** `400` (and no message is stored) when any id in `file_ids` is unknown, was
    uploaded by another user, or is already attached to a message.
