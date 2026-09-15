package com.ailegal.client;

import android.app.Activity;
import android.os.Bundle;
import android.view.View;
import android.widget.EditText;
import android.widget.Toast;

public class BackendSettingsActivity extends Activity {
    private EditText backendUrl;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_backend_settings);

        backendUrl = findViewById(R.id.backend_url);
        backendUrl.setText(BackendConfig.getBaseUrl(this));

        findViewById(R.id.save_button).setOnClickListener(this::save);
        findViewById(R.id.cancel_button).setOnClickListener(v -> finish());
        findViewById(R.id.chip_emulator).setOnClickListener(v ->
                backendUrl.setText(BackendConfig.EMULATOR_BASE_URL));
        findViewById(R.id.chip_lan).setOnClickListener(v ->
                backendUrl.setText(BackendConfig.DEFAULT_BASE_URL));
    }

    private void save(View view) {
        if (!BackendConfig.saveBaseUrl(this, backendUrl.getText().toString())) {
            Toast.makeText(this, R.string.invalid_server_address, Toast.LENGTH_SHORT).show();
            return;
        }
        setResult(RESULT_OK);
        Toast.makeText(this, R.string.server_address_saved, Toast.LENGTH_SHORT).show();
        finish();
    }
}
