package io.github.siziff.musloop;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // local plugin (finds musloop on the Wi-Fi), registered before the bridge starts
        registerPlugin(DiscoveryPlugin.class);
        registerPlugin(QrScanPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
