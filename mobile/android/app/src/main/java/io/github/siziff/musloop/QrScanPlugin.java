package io.github.siziff.musloop;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.google.mlkit.common.MlKitException;
import com.google.mlkit.vision.barcode.common.Barcode;
import com.google.mlkit.vision.codescanner.GmsBarcodeScanner;
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions;
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning;

/**
 * QR scanning through Google's code scanner (Google Play services):
 * the system shows its own camera UI and returns the text. No camera
 * permission, no scanner libraries inside the APK.
 */
@CapacitorPlugin(name = "QrScan")
public class QrScanPlugin extends Plugin {

    @PluginMethod
    public void scan(PluginCall call) {
        GmsBarcodeScannerOptions options = new GmsBarcodeScannerOptions.Builder()
                .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
                .enableAutoZoom()
                .build();
        GmsBarcodeScanner scanner = GmsBarcodeScanning.getClient(getActivity(), options);
        scanner.startScan()
                .addOnSuccessListener(barcode -> {
                    JSObject ret = new JSObject();
                    ret.put("text", barcode.getRawValue());
                    call.resolve(ret);
                })
                .addOnCanceledListener(() -> call.reject("canceled", "CANCELED"))
                .addOnFailureListener(e -> {
                    // no Google Play services / module still downloading on first use
                    String code = (e instanceof MlKitException) ? "MLKIT_" + ((MlKitException) e).getErrorCode() : "FAILED";
                    call.reject(e.getMessage() == null ? "scanner unavailable" : e.getMessage(), code);
                });
    }
}
