删掉本地 mp4 文件
把数据库里这集的状态改成非 uploaded（如 download_failed）
python -c "import sqlite3; conn=sqlite3.connect('xianni.sqlite3'); conn.execute(\"UPDATE episodes SET status='download_failed' WHERE episode=?\", (12,)); conn.commit(); print(conn.execute('SELECT episode,status FROM episodes WHERE episode=12').fetchone()); conn.close()"
然后删除本地文件（如果存在）：
del ".\downloads\仙逆第12集.mp4"

重新下载多集：

python -c "import sqlite3; conn=sqlite3.connect('xianni.sqlite3'); conn.execute('UPDATE episodes SET status=? WHERE episode IN (11,13,22)', ('download_failed',)); conn.commit(); print(list(conn.execute('SELECT episode,status FROM episodes WHERE episode IN (11,13,22)'))); conn.close()"



python main.py --upload

python main.py --download-only



telegram:
  api_id: '22088106'
  api_hash: 626778e1595a51170d64ed1b23f56350
  group_id: '-1003966238914'
  session_path: dongman.session
  topic_top_msg_id: null
  pindao: https://t.me/chmh_cn
  proxy:
    enabled: false
    host: 23.106.129.111
    port: 10800
    username: proxyuser
    password: 123456Aa!




    pip install FastTelethonhelper


    cd D:\atao\phyton\fasttelethon
python scripts/mark_shows_done.py --show-id wanmeishijie --show-id cangyuantu