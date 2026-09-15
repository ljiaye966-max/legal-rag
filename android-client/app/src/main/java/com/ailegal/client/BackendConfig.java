package com.ailegal.client;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;
import android.text.TextUtils;

final class BackendConfig {
    private static final String PREFS_NAME = "backend_config";
    private static final String KEY_BASE_URL = "base_url";

    // Web, mini program, and Android use the same server backend by default.
    static final String DEFAULT_BASE_URL = "http://193.112.28.40";
    private static final String LEGACY_DEFAULT_BASE_URL = "http://172.20.10.4:8001";

    // Android 模拟器访问宿主机后端使用的回环地址。
    static final String EMULATOR_BASE_URL = "http://10.0.2.2:8001";

    private BackendConfig() {
    }

    static String getBaseUrl(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        String saved = prefs.getString(KEY_BASE_URL, "");
        if (TextUtils.isEmpty(saved) || LEGACY_DEFAULT_BASE_URL.equals(normalize(saved))) {
            return DEFAULT_BASE_URL;
        }
        return normalize(saved);
    }

    static boolean saveBaseUrl(Context context, String input) {
        String normalized = normalize(input);
        Uri uri = Uri.parse(normalized);
        if (TextUtils.isEmpty(uri.getScheme()) || TextUtils.isEmpty(uri.getHost())) {
            return false;
        }
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit()
                .putString(KEY_BASE_URL, normalized)
                .apply();
        return true;
    }

    static String normalize(String value) {
        String result = value == null ? "" : value.trim();
        if (!result.startsWith("http://") && !result.startsWith("https://")) {
            result = "http://" + result;
        }
        while (result.endsWith("/")) {
            result = result.substring(0, result.length() - 1);
        }
        return result;
    }
}
