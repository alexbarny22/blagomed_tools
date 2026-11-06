<?php
/*
 * Вход в отчёт: форма с запоминанием на 30 дней (подписанная cookie).
 * Файл index.php собирается скриптом direct_report.py из этого шаблона и
 * dashboard_template.html — править index.php вручную не нужно.
 * Настройки (логин, хэш пароля, секрет подписи) лежат вне публичной папки.
 */
date_default_timezone_set('Asia/Barnaul');

$DR_ST  = json_decode(@file_get_contents('__SETTINGS_PATH__'), true);
$DR_U   = isset($DR_ST['report_user']) ? (string)$DR_ST['report_user'] : '';
$DR_PH  = isset($DR_ST['report_pass_hash']) ? (string)$DR_ST['report_pass_hash'] : '';
$DR_SEC = isset($DR_ST['key']) ? (string)$DR_ST['key'] : '';
$DR_DAYS = 30;   // срок «запомнить меня»

function dr_cookie_ok($u, $sec) {
    if ($u === '' || $sec === '' || empty($_COOKIE['drauth'])) return false;
    $p = explode('.', (string)$_COOKIE['drauth']);
    if (count($p) !== 2) return false;
    $exp = intval($p[0]);
    if ($exp < time()) return false;
    return hash_equals(hash_hmac('sha256', $u . '|' . $exp, $sec), $p[1]);
}

/* выход */
if (isset($_GET['exit'])) {
    setcookie('drauth', '', array('expires' => time() - 3600, 'path' => '/'));
    header('Location: ' . strtok($_SERVER['REQUEST_URI'], '?'));
    exit;
}

/* вход */
$DR_ERR = '';
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['dr_u'])) {
    if ($DR_U !== '' && $DR_PH !== ''
        && hash_equals($DR_U, (string)$_POST['dr_u'])
        && hash_equals($DR_PH, hash('sha256', (string)$_POST['dr_p']))) {
        $exp = time() + $DR_DAYS * 86400;
        $sig = hash_hmac('sha256', $DR_U . '|' . $exp, $DR_SEC);
        setcookie('drauth', $exp . '.' . $sig, array(
            'expires'  => $exp, 'path' => '/', 'httponly' => true, 'samesite' => 'Lax',
            'secure'   => !empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off'));
        header('Location: ' . strtok($_SERVER['REQUEST_URI'], '?'));
        exit;
    }
    $DR_ERR = 'Неверный логин или пароль';
    sleep(1);   // против перебора
}

if (!dr_cookie_ok($DR_U, $DR_SEC)) {
    header('Cache-Control: no-store');
    ?><!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Вход — Яндекс Директ, отчёт</title>
<style>
:root{color-scheme:light dark;--bg:#f4f3f0;--surface:#fcfcfb;--line:#e2e1db;--text:#0b0b0b;--text-2:#52514e;--accent:#2a78d6;--err:#b42323}
@media (prefers-color-scheme:dark){:root{--bg:#121211;--surface:#1a1a19;--line:#33332f;--text:#f0efec;--text-2:#c3c2b7;--accent:#3987e5;--err:#f07070}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);
  color:var(--text);font:14px/1.45 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;padding:16px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:28px;width:100%;max-width:340px}
h1{font-size:18px;margin:0 0 4px}
.sub{color:var(--text-2);font-size:13px;margin-bottom:18px}
label{display:block;font-size:13px;color:var(--text-2);margin:12px 0 4px}
input{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:8px;font:inherit;background:var(--bg);color:var(--text)}
input:focus{outline:2px solid var(--accent);outline-offset:-1px}
button{margin-top:18px;width:100%;padding:10px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
.err{background:var(--err);color:#fff;border-radius:8px;padding:8px 10px;margin-bottom:4px;font-size:13px}
.note{margin-top:14px;font-size:12px;color:var(--text-2)}
</style>
</head>
<body>
<form class="card" method="post" autocomplete="on">
  <h1>Яндекс Директ — отчёт</h1>
  <div class="sub">Вход для просмотра статистики</div>
  <?php if ($DR_ERR) echo '<div class="err">' . htmlspecialchars($DR_ERR) . '</div>'; ?>
  <label for="u">Логин</label>
  <input id="u" name="dr_u" type="text" autocomplete="username" required autofocus>
  <label for="p">Пароль</label>
  <input id="p" name="dr_p" type="password" autocomplete="current-password" required>
  <button type="submit">Войти</button>
  <div class="note">Запоминает вход на <?php echo $DR_DAYS; ?> дней — повторно вводить не нужно. Браузер может предложить сохранить пароль — соглашайтесь.</div>
</form>
</body>
</html><?php
    exit;
}

/* скачивание Excel — только после входа (прямой доступ к *.xlsx закрыт в .htaccess) */
if (isset($_GET['xls'])) {
    $f = basename((string)$_GET['xls']);
    if (preg_match('/^direct_\d{4}-\d{2}-\d{2}\.xlsx$/', $f) && is_file(__DIR__ . '/' . $f)) {
        header('Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
        header('Content-Disposition: attachment; filename="' . $f . '"');
        header('Content-Length: ' . filesize(__DIR__ . '/' . $f));
        header('Cache-Control: no-store');
        readfile(__DIR__ . '/' . $f);
    } else {
        http_response_code(404);
        echo 'not found';
    }
    exit;
}
/* ниже — сам дашборд (добавляется скриптом) */
?>
