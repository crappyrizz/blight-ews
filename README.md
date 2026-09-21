# Blight Early-Warning System (Kinangop)

Final-year BSc ICS project, Strathmore University.

A decision-support system that forecasts potato late blight risk for a smallholder farm in Kinangop (Nyandarua, Kenya) from microclimate data, confirms leaf symptoms with a CNN, fuses the two, and alerts the farmer by SMS.

## Layout

| Folder      | Contents                                              |
|-------------|-------------------------------------------------------|
| `backend/`  | FastAPI app and its pytest tests                      |
| `ml/`       | Weather fetching, feature building, model training/eval |
| `tools/`    | Sensor-node simulator and database seeding            |
| `app/`      | Flutter mobile app                                    |
| `firmware/` | ESP32 + SHT31 node firmware (later)                   |
| `models/`   | Exported models (binaries are gitignored)             |
| `data/`     | Local data, gitignored                                |
| `docs/`     | Evaluation tables and figures                         |

See `CLAUDE.md` for the full design notes.

_Setup instructions will be added as components are built._
