# Android Client

This is a standalone native Android wrapper for the existing AI legal case reasoning system. It does not modify the backend, the mini-program, the knowledge base, or `.env`.

## What it preserves

The app displays the existing backend system in a native Android WebView. Existing login, phone/SMS registration, knowledge-base management, case reasoning, history, comparison, and DashScope RAG features remain served by the original backend.

The Android app never contains Aliyun AccessKey values or DashScope keys. Keep those values in the backend `.env` only.

## Open in Android Studio

1. Open this `android-client` folder in Android Studio.
2. Use JDK 17 or newer and install Android SDK Platform 35.
3. Let Android Studio complete the Gradle sync, then run on an emulator or Android device.

## Backend address

Open `Server Settings` in the Android app to change the address without editing source code.

- Android emulator: `http://10.0.2.2:8001`
- Physical phone: `http://<computer-LAN-IP>:8001`

For a physical phone, the FastAPI service must listen on `0.0.0.0:8001`, and the phone and computer must be on the same Wi-Fi. Windows Firewall must allow Python/Uvicorn on private networks.

## Local HTTP and production

Cleartext HTTP is enabled for local development. For a release build, deploy the backend to an HTTPS domain and enter that HTTPS URL in `Server Settings`.
