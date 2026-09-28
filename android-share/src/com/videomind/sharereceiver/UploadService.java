package com.videomind.sharereceiver;

import android.app.Service;
import android.content.Intent;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;
import android.widget.Toast;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * 后台上传服务：接收 ClipCaptureActivity 传来的抖音链接，POST 到知识库 API，
 * 用 Toast 反馈结果，然后自停。
 */
public class UploadService extends Service {

    private static final String TAG = "VideoMindUpload";
    private static final String API_URL = "http://127.0.0.1:8000/api/v1/captures";

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        final String url = intent == null ? null : intent.getStringExtra("url");
        final String raw = intent == null ? null : intent.getStringExtra("raw");
        if (url == null || url.isEmpty()) {
            stopSelf();
            return START_NOT_STICKY;
        }

        new Thread(() -> {
            String msg;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(API_URL).openConnection();
                c.setRequestMethod("POST");
                c.setConnectTimeout(5000);
                c.setReadTimeout(8000);
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json; charset=utf-8");

                String body = "{\"url\":\"" + esc(url) + "\",\"raw_text\":\""
                        + esc(raw == null ? "" : raw)
                        + "\",\"user_note\":\"\",\"source\":\"accessibility\"}";
                try (OutputStream os = c.getOutputStream()) {
                    os.write(body.getBytes(StandardCharsets.UTF_8));
                }
                int code = c.getResponseCode();
                msg = (code == 202 || code == 200) ? "✓ 链接已入队，等待解析" : "收藏失败 HTTP " + code;
                Log.i(TAG, "上传结果 code=" + code);
            } catch (Exception e) {
                msg = "收藏失败：" + e.getClass().getSimpleName();
                Log.e(TAG, "上传失败", e);
            }
            final String m = msg;
            new Handler(Looper.getMainLooper()).post(
                    () -> Toast.makeText(getApplicationContext(), m, Toast.LENGTH_SHORT).show());
            stopSelf();
        }).start();

        return START_NOT_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private static String esc(String s) {
        StringBuilder b = new StringBuilder();
        for (char ch : s.toCharArray()) {
            switch (ch) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                default:
                    if (ch < 0x20) b.append(String.format("\\u%04x", (int) ch));
                    else b.append(ch);
            }
        }
        return b.toString();
    }
}
