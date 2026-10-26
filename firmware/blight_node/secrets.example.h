// Copy this file to secrets.h (gitignored) and fill it in. Never commit secrets.h.
//
// Wokwi: add a file named secrets.h to the project with these lines. The free
// Wokwi project is public, so only ever put the wokwi-01 key there: it is
// treated as public and can only add readings to wokwi-01.
// Real node: the node-01 / node-02 key from device_keys.local.env.
#pragma once

#define WIFI_SSID "Wokwi-GUEST"  // real node: your 2.4 GHz phone hotspot
#define WIFI_PASSWORD ""
#define DEVICE_ID "wokwi-01"
#define DEVICE_KEY "paste-the-device-key-here"
#define API_URL "https://blight-ews-api.onrender.com/readings"
