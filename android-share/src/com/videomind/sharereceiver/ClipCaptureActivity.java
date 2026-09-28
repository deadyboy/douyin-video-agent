package com.videomind.sharereceiver;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.util.Log;

import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 透明 Activity：由无障碍服务拉起，短暂取得焦点以读取剪贴板。
 *
 * 生命周期极短：读板 → 把链接交给 UploadService 后台上传 → 立即 finish()，
 * 用户几乎无感地回到抖音。
 */
public class ClipCaptureActivity extends Activity {

    private static final String TAG = "VideoMindClip";
    private static final Pattern DOUYIN = Pattern.compile(
            "https?://v\\.douyin\\.com/[A-Za-z0-9._~%\\-/?#=&]+");

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        String text = readClipboard();
        Log.i(TAG, "剪贴板内容: " + text);

        Matcher m = DOUYIN.matcher(text == null ? "" : text);
        if (m.find()) {
            String url = m.group(0);
            Intent svc = new Intent(this, UploadService.class);
            svc.putExtra("url", url);
            svc.putExtra("raw", text);
            startService(svc);
            Log.i(TAG, "已派发上传: " + url);
        } else {
            Log.i(TAG, "剪贴板里没有抖音短链，忽略");
        }
        finish();
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
            Log.e(TAG, "读取剪贴板失败", e);
            return "";
        }
    }
}
