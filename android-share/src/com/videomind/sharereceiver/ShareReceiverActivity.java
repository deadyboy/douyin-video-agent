package com.videomind.sharereceiver;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * VideoMind Capture（v0.2）
 *
 * 打开即读剪贴板 → 提取抖音短链 → POST 到知识库 API → 显示结果。
 * API 经 `adb reverse tcp:8000 tcp:8000` 映射到开发机。
 *
 * 另保留 ACTION_SEND 入口（若将来有 App 走系统面板可直接接）。
 */
public class ShareReceiverActivity extends Activity {

    private static final String TAG = "VideoMindCapture";
    // 经 adb reverse 映射到 PC 的 API
    private static final String API_URL = "http://127.0.0.1:8000/api/v1/captures";
    // App 端预判：剪贴板里有没有抖音短链（正式提取在服务端做）
    private static final Pattern DOUYIN = Pattern.compile(
            "https?://v\\.douyin\\.com/[A-Za-z0-9._~%\\-/?#=&]+");

    private TextView status;
    private TextView detail;
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(buildUi());

        Intent intent = getIntent();
        if (intent != null && Intent.ACTION_SEND.equals(intent.getAction())) {
            // 从分享意图进来的文本
            CharSequence shared = intent.getCharSequenceExtra(Intent.EXTRA_TEXT);
            String text = shared == null ? "" : shared.toString();
            if (text.isEmpty() && intent.getClipData() != null
                    && intent.getClipData().getItemCount() > 0) {
                CharSequence t = intent.getClipData().getItemAt(0).getText();
                text = t == null ? "" : t.toString();
            }
            capture(text, "share_intent");
        } else {
            // 打开即读剪贴板
            capture(readClipboard(), "clipboard");
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (intent != null && Intent.ACTION_SEND.equals(intent.getAction())) {
            CharSequence shared = intent.getCharSequenceExtra(Intent.EXTRA_TEXT);
            capture(shared == null ? "" : shared.toString(), "share_intent");
        }
    }

    private LinearLayout buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(48, 96, 48, 48);

        TextView title = new TextView(this);
        title.setText("VideoMind 收藏");
        title.setTextSize(22f);
        root.addView(title);

        status = new TextView(this);
        status.setTextSize(16f);
        status.setPadding(0, 32, 0, 16);
        status.setText("读取中…");
        root.addView(status);

        Button again = new Button(this);
        again.setText("重新读取剪贴板");
        again.setOnClickListener(v -> capture(readClipboard(), "clipboard"));
        root.addView(again);

        ScrollView sv = new ScrollView(this);
        detail = new TextView(this);
        detail.setTextIsSelectable(true);
        detail.setTextSize(13f);
        sv.addView(detail);
        root.addView(sv, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        return root;
    }

    private String readClipboard() {
        try {
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm == null || !cm.hasPrimaryClip() || cm.getPrimaryClip() == null) return "";
            ClipData clip = cm.getPrimaryClip();
            if (clip.getItemCount() == 0) return "";
            CharSequence t = clip.getItemAt(0).getText();
            return t == null ? "" : t.toString();
        } catch (Exception e) {
            Log.e(TAG, "clipboard read failed", e);
            return "";
        }
    }

    private void capture(String rawText, String source) {
        if (rawText == null || rawText.isEmpty()) {
            setStatus("剪贴板为空", "先在抖音里点「分享 → 复制链接」");
            return;
        }
        Matcher m = DOUYIN.matcher(rawText);
        if (!m.find()) {
            setStatus("剪贴板里没有抖音链接",
                    "当前内容：\n" + rawText.substring(0, Math.min(120, rawText.length())));
            return;
        }
        String url = m.group(0);
        setStatus("发现抖音链接，正在收藏…", url);
        postAsync(url, rawText, source);
    }

    private void postAsync(String url, String rawText, String source) {
        new Thread(() -> {
            String result;
            boolean ok = false;
            try {
                URL u = new URL(API_URL);
                HttpURLConnection c = (HttpURLConnection) u.openConnection();
                c.setRequestMethod("POST");
                c.setConnectTimeout(5000);
                c.setReadTimeout(8000);
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json; charset=utf-8");

                String body = "{\"url\":\"" + esc(url) + "\",\"raw_text\":\"" + esc(rawText)
                        + "\",\"user_note\":\"\",\"source\":\"" + esc(source) + "\"}";
                try (OutputStream os = c.getOutputStream()) {
                    os.write(body.getBytes(StandardCharsets.UTF_8));
                }
                int code = c.getResponseCode();
                InputStream is = (code >= 200 && code < 300) ? c.getInputStream() : c.getErrorStream();
                StringBuilder sb = new StringBuilder();
                if (is != null) {
                    try (BufferedReader br = new BufferedReader(
                            new InputStreamReader(is, StandardCharsets.UTF_8))) {
                        String line;
                        while ((line = br.readLine()) != null) sb.append(line);
                    }
                }
                result = "HTTP " + code + "\n" + sb;
                ok = (code == 202 || code == 200);
            } catch (Exception e) {
                result = "请求失败：" + e;
            }
            final String r = result;
            final boolean fok = ok;
            ui.post(() -> {
                if (fok) {
                    setStatus("✓ 已加入知识库", r);
                } else {
                    setStatus("✗ 收藏失败（检查 API 与 adb reverse）", r);
                }
            });
        }).start();
    }

    private void setStatus(String s, String d) {
        ui.post(() -> {
            status.setText(s);
            detail.setText(d);
        });
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
