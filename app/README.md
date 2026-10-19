# blight_ews (Flutter app)

The farmer's Android app for the Blight Early-Warning System: login, dashboard, leaf capture, diagnosis, history and symptom reports.

How to run it on a phone (Wi-Fi or USB), the server address and the screens are described in the main README: [Mobile app](../README.md#mobile-app-app-flutter-android).

```powershell
flutter analyze
flutter test
flutter run --dart-define=API_BASE_URL=http://<PC-IP>:8000
```
