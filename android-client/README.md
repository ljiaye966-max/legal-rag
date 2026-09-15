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

Open `Server Settings` in the Android app to change the address without editing source code. The current default points to the shared Tencent Cloud backend.

- Shared server: `http://193.112.28.40`
- Android emulator or physical phone: use the shared server above, or change it in `Server Settings`.

The shared server is exposed through Nginx on port 80, so the phone and computer do not need to be on the same Wi-Fi.

## Local HTTP and production

Cleartext HTTP is enabled for local development. For a release build, deploy the backend to an HTTPS domain and enter that HTTPS URL in `Server Settings`.
