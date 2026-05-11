-- episode_jobs 手工改状态
-- 数据库路径：config.yaml -> database.path（默认 data/app_state.sqlite）
--
-- 用法 A —— DB Browser：打开数据库 → 「执行 SQL」→ 复制下面「一整条」UPDATE 执行（改好 show_id、episode）。
-- 用法 B —— 命令行（项目根目录，装过 sqlite3 CLI）:
--   sqlite3 data/app_state.sqlite "UPDATE episode_jobs SET download_status='downloaded', upload_status='', updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE show_id='panlong' AND episode=1;"
--
-- 字段含义（与 app/store.py / download_worker 一致）:
--   download_status = 'downloaded'  → 跳过下载（且本地需存在对应 mp4）
--   upload_status = ''              → 未上传，run.py 仍会尝试上传
--   upload_status = 'uploaded'      → 已上传，上传环节跳过

-- 查看（可先执行）
SELECT show_id, episode, url, download_status, upload_status, updated_at
FROM episode_jobs
ORDER BY show_id, episode;

---------------------------------------------------------------------------
-- 【模板 A】单集：已下载，还要上传（upload 为空）
-- 把 panlong、1 改成你的 show_id、集号。
---------------------------------------------------------------------------
/*
UPDATE episode_jobs
SET
  download_status = 'downloaded',
  upload_status = '',
  updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE show_id = 'panlong'
  AND episode = 1;
*/

---------------------------------------------------------------------------
-- 【模板 B】单集：已下载且已上传过（以后不再传）
---------------------------------------------------------------------------
/*
UPDATE episode_jobs
SET
  download_status = 'downloaded',
  upload_status = 'uploaded',
  updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE show_id = 'panlong'
  AND episode = 1;
*/

---------------------------------------------------------------------------
-- 【模板 C】整部番所有集：已下载、待上传（慎用）
---------------------------------------------------------------------------
/*
UPDATE episode_jobs
SET
  download_status = 'downloaded',
  upload_status = '',
  updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE show_id = 'panlong';
*/

---------------------------------------------------------------------------
-- 【模板 D】整部番所有集：已下载、已上传（慎用）
---------------------------------------------------------------------------
/*
UPDATE episode_jobs
SET
  download_status = 'downloaded',
  upload_status = 'uploaded',
  updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE show_id = 'panlong';
*/
