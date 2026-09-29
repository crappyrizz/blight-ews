# Firmware contract: sending readings to the backend

For whoever writes the ESP32 + SHT31 firmware. This is the exact HTTP
behaviour the backend expects, and `tools/simulate_node.py` follows the same
contract so the backend can be tested before the hardware exists.

Written against the backend as of the sensor-ingest task. The limits below
live in `backend/schemas.py`; if they change, this document changes with them.

---

## 1. What the node does, in one paragraph

Every hour the node measures temperature and humidity and stores the reading
in local flash. Whenever it has a connection, it POSTs everything it has not
yet had accepted, in one batch, and clears from its buffer only what the
server confirms. Coverage at the plot is weak, so being offline for hours or
days is normal, not an error.

---

## 2. Endpoint

```
POST /nodes/{node_id}/readings
```

| | |
|---|---|
| `node_id` | The node's own ID, e.g. `NODE-E30CEA4721CE`. Issued with the key; store both in flash |
| `Content-Type` | `application/json` |
| `X-Node-Key` | The node's API key, issued once at provisioning |

The node has **no** login and no farmer token. Its key authorises exactly one
thing: sending readings for its own `node_id`. Using it for another node's ID
fails with 401.

The server stores only a hash of the key, so a lost key cannot be recovered,
only replaced. Keep it out of any source file that goes into version control.

---

## 3. Request body

```json
{
  "readings": [
    {"timestamp": "2026-09-22T03:00:00Z", "temperature": 12.4, "humidity": 93.1, "leaf_wetness": null},
    {"timestamp": "2026-09-22T04:00:00Z", "temperature": 12.1, "humidity": 95.0, "leaf_wetness": 0.42}
  ]
}
```

| Field | Type | Rules |
|---|---|---|
| `timestamp` | string | ISO 8601 **with a timezone**. Send UTC with a trailing `Z`. A time with no zone is rejected |
| `temperature` | number | °C, between **-10 and 50** inclusive |
| `humidity` | number | % relative humidity, between **0 and 100** inclusive |
| `leaf_wetness` | number or `null` | Optional. Send `null` (or leave the field out) when no leaf-wetness sensor is fitted |

Rules that matter:

- **At most 500 readings per batch.** More gets `413` and **nothing is
  stored**; split the buffer into batches of 500 or fewer.
- **Unknown field names are rejected.** A typo such as `temprature` is
  reported rather than silently ignored, so firmware bugs surface early.
- **Any order is fine.** The server sorts by timestamp; no need to sort first.
- **Old readings are fine.** There is no lower limit on age, so a week-old
  backlog uploads normally.
- **A timestamp more than 10 minutes in the future is rejected.** That
  tolerance exists for clock drift, not for a wrong clock: see §7.

---

## 4. Response

`200 OK` with:

```json
{"accepted": 18, "duplicates": 6, "rejected": [{"index": 4, "reason": "temperature: Input should be less than or equal to 50"}]}
```

| Field | Meaning |
|---|---|
| `accepted` | Rows newly stored |
| `duplicates` | Rows the server already had, or repeated within this batch. **Not an error** |
| `rejected` | Rows that failed validation. `index` is the row's position in the array you sent, counting from 0 |

**A 200 means the server is done with every row in that batch**: stored,
already had it, or refused it. The node may clear all of them from its
buffer. A rejected row will never be accepted on a retry, because it is the
data that is wrong, so retrying it forever would block the buffer.

Log rejections if you can. They mean a firmware or sensor fault.

---

## 5. Status codes

| Code | Meaning | What the firmware should do |
|---|---|---|
| `200` | Batch processed (see the body) | Clear those readings from the buffer |
| `401` | Missing, wrong, or unknown `X-Node-Key` / `node_id` | Do **not** retry in a loop. Keep buffering, retry slowly (e.g. hourly); the key or ID needs fixing by hand |
| `413` | More than 500 readings in one batch | Split the batch and resend. Nothing was stored |
| `422` | The envelope itself is malformed (not an object, `readings` missing or not an array) | Firmware bug. Do not retry unchanged |
| `5xx`, timeout, no connection | Server or network problem | Keep the readings and retry with backoff (§6) |

Anything other than 2xx means **keep the data**.

---

## 6. Retry and buffer behaviour

1. **Buffer first, send second.** Write the reading to flash as soon as it is
   measured, before any network attempt. A failed upload must never lose a
   reading.
2. **Hold at least 7 days** of hourly readings: 168 rows, comfortably within
   an ESP32's flash. More is better; the server accepts old data.
3. **Flush on a schedule and on reconnect**, e.g. try every hour after
   measuring, and again whenever the network comes back.
4. **Exponential backoff with a cap** on failures: 1, 2, 4, 8… minutes up to
   about 30 minutes, with a little randomness so repeated failures do not
   line up with anything else.
5. **Never retry faster than once a minute**, even after an immediate error.
6. **Delete only what a 200 covers.** If the response is lost in transit,
   keep the rows and send them again: the server skips duplicates, so a
   replayed batch is harmless and returns `accepted: 0`.
7. **If the buffer fills up**, drop the *oldest* readings first. Recent
   weather matters more for a 24-48h forecast than a filled-in gap.
8. **One batch at a time.** Do not send a second batch before the first
   answers; the node has nothing to gain from parallel requests.

---

## 7. Clock

The timestamp is the node's own claim about when it measured, and the whole
Hutton calculation counts hours per local day, so a wrong clock silently
corrupts the risk score.

- Sync with NTP on boot and at least once a day.
- If the clock has never been set, **do not send invented timestamps**. Keep
  measuring into the buffer and send once the time is known, tagging the
  readings with the corrected times if you can, or discarding them if you
  cannot.
- Send UTC, not local time. The backend converts to Africa/Nairobi itself,
  only where a local calendar day is needed.

---

## 8. Reading data back (not for the firmware)

The mobile app uses a farmer's JWT, not a node key:

```
GET /nodes/{node_id}/readings?from=2026-09-22T00:00:00Z&to=2026-09-23T00:00:00Z
Authorization: Bearer <token>
```

`from` is inclusive, `to` is exclusive, results are ordered by timestamp, and
a farmer may only read their own nodes.

---

## 9. Worked example

```
POST /nodes/NODE-E30CEA4721CE/readings
Content-Type: application/json
X-Node-Key: 7f3c9e1a...   (43 characters)

{"readings":[
  {"timestamp":"2026-09-22T03:00:00Z","temperature":12.4,"humidity":93.1,"leaf_wetness":null},
  {"timestamp":"2026-09-22T04:00:00Z","temperature":12.1,"humidity":95.0,"leaf_wetness":null}
]}
```

```
200 OK
{"accepted":2,"duplicates":0,"rejected":[]}
```

Send the same batch again and you get `{"accepted":0,"duplicates":2,"rejected":[]}`,
which is the whole point: a retry after an unclear failure costs nothing.
