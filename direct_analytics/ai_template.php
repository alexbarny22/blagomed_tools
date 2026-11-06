<?php
/*
 * ИИ-советник: экспертный анализ нейросетью ПО КНОПКЕ на странице отчёта.
 * Файл создаётся скриптом direct_report.py — править его вручную не нужно.
 * Нейросеть вызывается только из этого файла и только по явному запросу со страницы;
 * ночной отчёт её не вызывает и токены не тратит.
 * Нейросеть ничего не меняет в Директе — только текстовые рекомендации.
 */
date_default_timezone_set('Asia/Barnaul');
@set_time_limit(300);
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

$SETTINGS = '__SETTINGS_PATH__';

function out($a, $code = 200) { http_response_code($code); echo json_encode($a, JSON_UNESCAPED_UNICODE); exit; }
function rj($p, $d) { if (!is_file($p)) return $d; $x = json_decode(file_get_contents($p), true); return $x === null ? $d : $x; }

$S = rj($SETTINGS, null);
if (!$S) out(['ok' => false, 'error' => 'ИИ-советник не настроен — запустите direct_report.py с AI_ENABLED = True'], 500);
if (empty($S['api_key']) || empty($S['base_url']) || empty($S['model']))
    out(['ok' => false, 'error' => 'Не заполнены AI_BASE_URL / AI_API_KEY / AI_MODEL в config.py'], 500);

$in = json_decode(file_get_contents('php://input'), true);
if (!$in) $in = $_GET;
if (!isset($in['key']) || !hash_equals($S['key'], (string)$in['key'])) out(['ok' => false, 'error' => 'Неверный ключ страницы. Обновите страницу.'], 403);
$action = isset($in['action']) ? $in['action'] : 'ask';

$payload = rj($S['payload'], null);
if (!$payload || empty($payload['summaries']))
    out(['ok' => false, 'error' => 'Нет сводки статистики — дождитесь ночного запуска отчёта или запустите run.sh'], 500);
$date = isset($payload['date']) ? $payload['date'] : '';
$periods = isset($payload['periods']) ? array_map('intval', $payload['periods']) : [30];

/* ---------- кэш: повторный клик в тот же день по тому же периоду бесплатный ---------- */
function cache_read() { global $S; return rj($S['cache'], []); }
function cache_write($c) {
    global $S;
    $fp = fopen($S['cache'], 'c+');
    flock($fp, LOCK_EX);
    ftruncate($fp, 0); rewind($fp);
    fwrite($fp, json_encode($c, JSON_UNESCAPED_UNICODE));
    fflush($fp); flock($fp, LOCK_UN); fclose($fp);
}

if ($action === 'last') {   // что уже посчитано для свежей сводки — без вызова нейросети
    $c = cache_read();
    $have = isset($c[$date]) && is_array($c[$date]) ? $c[$date] : [];
    $res = [];
    foreach ($have as $d => $ai) $res[$d] = $ai;
    out(['ok' => true, 'date' => $date, 'periods' => $periods, 'results' => $res]);
}

$days = isset($in['days']) ? intval($in['days']) : 0;
if (!in_array($days, $periods)) out(['ok' => false, 'error' => 'Период должен быть одним из: ' . implode(', ', $periods)], 400);

$c = cache_read();
if (isset($c[$date][$days])) out(['ok' => true, 'cached' => true, 'days' => $days, 'date' => $date, 'ai' => $c[$date][$days]]);

/* ---------- вызов нейросети ---------- */
$body = json_encode([
    'model' => $S['model'],
    'messages' => [
        ['role' => 'system', 'content' => $payload['system']],
        ['role' => 'user', 'content' => $payload['summaries'][(string)$days]],
    ],
    'temperature' => 0.2,
    'max_tokens' => intval($S['max_tokens']),
], JSON_UNESCAPED_UNICODE);

$ch = curl_init(rtrim($S['base_url'], '/') . '/chat/completions');
curl_setopt_array($ch, [CURLOPT_POST => true, CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT => max(30, intval($S['timeout'])),
    CURLOPT_HTTPHEADER => ['Authorization: ' . $S['scheme'] . ' ' . $S['api_key'],
                           'Content-Type: application/json; charset=utf-8'],
    CURLOPT_POSTFIELDS => $body]);
$resp = curl_exec($ch);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
if ($resp === false) { $e = curl_error($ch); curl_close($ch); out(['ok' => false, 'error' => 'Нет связи с API нейросети: ' . $e], 502); }
curl_close($ch);
$data = json_decode($resp, true);
if ($code !== 200 || !$data) {
    $msg = isset($data['error']['message']) ? $data['error']['message'] : substr($resp, 0, 300);
    out(['ok' => false, 'error' => 'HTTP ' . $code . ' от API нейросети: ' . $msg], 502);
}
$text = isset($data['choices'][0]['message']['content']) ? trim($data['choices'][0]['message']['content']) : '';
if ($text === '') out(['ok' => false, 'error' => 'Нейросеть вернула пустой ответ — попробуйте ещё раз'], 502);

/* ---------- разбор ответа ---------- */
$ai = json_decode($text, true);
if (!is_array($ai) && preg_match('/\{.*\}/s', $text, $m)) $ai = json_decode($m[0], true);
if (!is_array($ai)) $ai = ['summary' => $text, 'recommendations' => [], 'watchlist' => []];

$recs = [];
foreach ((isset($ai['recommendations']) ? $ai['recommendations'] : []) as $r) {
    if (!is_array($r) || empty($r['title'])) continue;
    $recs[] = ['title' => mb_substr((string)$r['title'], 0, 200),
               'details' => mb_substr(isset($r['details']) ? (string)$r['details'] : '', 0, 2000),
               'priority' => mb_strtolower(isset($r['priority']) ? (string)$r['priority'] : 'medium'),
               'category' => mb_substr(isset($r['category']) ? (string)$r['category'] : 'другое', 0, 40),
               'expected_effect' => mb_substr(isset($r['expected_effect']) ? (string)$r['expected_effect'] : '', 0, 300)];
    if (count($recs) >= 8) break;
}
$watch = [];
foreach ((isset($ai['watchlist']) ? $ai['watchlist'] : []) as $w) { $watch[] = mb_substr((string)$w, 0, 200); if (count($watch) >= 6) break; }
$ai = ['summary' => mb_substr(isset($ai['summary']) ? (string)$ai['summary'] : '', 0, 3000),
       'recommendations' => $recs, 'watchlist' => $watch,
       'provider' => $S['model'], 'days' => $days, 'date' => $date];

$c[$date] = isset($c[$date]) && is_array($c[$date]) ? $c[$date] : [];
$c[$date][$days] = $ai;
foreach (array_keys($c) as $k) if ($k !== $date) unset($c[$k]);   // храним только свежую сводку
cache_write($c);
out(['ok' => true, 'cached' => false, 'days' => $days, 'date' => $date, 'ai' => $ai]);
