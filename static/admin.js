// Логика админки. Извлечено из inline <script> admin.html без изменений —
// порядок исполнения тот же (внешний script с src выполняется на месте подключения).
// refreshIcons() определяется отдельным inline-скриптом после загрузки Lucide (см. admin.html).
        // ---------------- Общее ----------------
        function escapeHtml(str) {
            if (str === null || str === undefined) return '';
            const div = document.createElement('div');
            div.textContent = String(str);
            return div.innerHTML;
        }

        /** fetch с единой обработкой протухшей сессии: 401 -> обратно на форму входа. */
        function api(url, options) {
            return fetch(url, options).then(res => {
                if (res.status === 401) {
                    window.location.href = '/login';
                    throw new Error('Сессия истекла');
                }
                return res;
            });
        }

        function apiJson(url, options) {
            return api(url, options).then(res => res.json().then(data => ({ ok: res.ok, data })));
        }

        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
                tab.classList.add('active');
                document.getElementById(`pane-${tab.dataset.tab}`).classList.add('active');
                if (tab.dataset.tab === 'builder') ensureBuilderLoaded();
                if (tab.dataset.tab === 'plantexts') ensurePlanTextsLoaded();
                if (tab.dataset.tab === 'employees') ensureEmployeesLoaded();
                if (tab.dataset.tab === 'questions') loadQuestions();
                if (tab.dataset.tab === 'docs') loadStageBoard();
            });
        });

        let currentUser = null;
        let isOwner = false;

        api('/api/me').then(r => r.json()).then(me => {
            currentUser = me;
            isOwner = me.role === 'owner';
            const roleTitle = isOwner ? 'суперадмин' : 'администратор';
            document.getElementById('whoami').innerHTML = `<i data-lucide="user"></i> ${escapeHtml(me.full_name || me.username)} · ${roleTitle}`;
            if (employeesCache.length) renderEmployees(employeesCache);
            if (docsCache.length) renderDocuments(docsCache);   // группировка по владельцу — только суперадмину
            // «Переанализировать всё» задевает документы всех администраторов -> только суперадмину
            if (isOwner) document.getElementById('btn-reanalyze-all').style.display = '';
        }).catch(() => {});

        function logout() {
            fetch('/api/logout', { method: 'POST' }).finally(() => { window.location.href = '/login'; });
        }

        function openPasswordDialog() {
            document.getElementById('password-error').style.display = 'none';
            document.getElementById('old-password').value = '';
            document.getElementById('new-password').value = '';
            document.getElementById('password-dialog').showModal();
        }

        function submitPasswordChange() {
            const errorBox = document.getElementById('password-error');
            apiJson('/api/password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    old_password: document.getElementById('old-password').value,
                    new_password: document.getElementById('new-password').value,
                }),
            }).then(({ ok, data }) => {
                if (!ok) {
                    errorBox.textContent = data.detail || 'Не удалось сменить пароль';
                    errorBox.style.display = 'block';
                    return;
                }
                alert('Пароль изменён. Войдите заново.');
                window.location.href = '/login';
            });
        }

        // ---------------- База знаний ----------------
        const dropzone = document.getElementById('dropzone');
        const fileInput = document.getElementById('file-input');
        const docList = document.getElementById('doc-list');
        const folderGrid = document.getElementById('folder-grid');
        const uploadProgress = document.getElementById('upload-progress');

        dropzone.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', () => {
            if (fileInput.files.length) uploadFiles(fileInput.files);
        });
        ['dragenter', 'dragover'].forEach(evt => {
            dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
        });
        ['dragleave', 'drop'].forEach(evt => {
            dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove('dragover'); });
        });
        dropzone.addEventListener('drop', (e) => {
            if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
        });

        // Прогресс по каждому файлу: каноничное имя -> строка статуса. Файлы обрабатываются
        // фоновой очередью на сервере. Статус тянем из durable-реестра GET /documents
        // (переживает рестарт и работу в несколько uvicorn-воркеров), а не из джоб-стора
        // в памяти процесса.
        const uploadStatus = {};
        const pendingDocs = new Set();   // каноничные имена, ждущие завершения индексации

        function renderUploadStatus() {
            const rows = Object.entries(uploadStatus);
            uploadProgress.innerHTML = rows.length
                ? rows.map(([name, line]) => `<div>${escapeHtml(name)}: ${line}</div>`).join('')
                : '';
        }

        function uploadFiles(fileList) {
            Array.from(fileList).forEach(uploadFile);
            fileInput.value = '';
        }

        function uploadFile(file) {
            const formData = new FormData();
            formData.append('file', file);
            uploadStatus[file.name] = 'загрузка...';
            renderUploadStatus();

            apiJson('/documents/upload', { method: 'POST', body: formData })
                .then(({ ok, data }) => {
                    if (!ok) {
                        uploadStatus[file.name] = `${escapeHtml(data.detail || 'ошибка загрузки')}`;
                        renderUploadStatus();
                        return;
                    }
                    // Каноничное имя из реестра (санитайзинг мог изменить исходное) —
                    // по нему сверяемся со списком /documents.
                    const canonical = data.filename || file.name;
                    delete uploadStatus[file.name];
                    uploadStatus[canonical] = 'в очереди на обработку...';
                    pendingDocs.add(canonical);
                    renderUploadStatus();
                    bumpDocPolling();
                })
                .catch(err => {
                    uploadStatus[file.name] = `${escapeHtml(err.message)}`;
                    renderUploadStatus();
                });
        }

        // Сверяем ожидающие файлы с durable-реестром /documents: обновляем строку
        // статуса, а по завершении (indexed/error) убираем её и обновляем счётчики папок.
        function reconcilePending(docs) {
            if (!pendingDocs.size) return;
            const byName = {};
            docs.forEach(d => { byName[d.filename] = d; });
            let settled = false;
            pendingDocs.forEach(name => {
                const doc = byName[name];
                if (!doc) return;   // ещё не появился в реестре
                if (doc.status === 'indexed' || doc.status === 'error') {
                    delete uploadStatus[name];   // готово/ошибка уже видно в списке документов ниже
                    pendingDocs.delete(name);
                    settled = true;
                } else if (doc.status === 'processing') {
                    uploadStatus[name] = 'разбор docling → чанкинг → эмбеддинги...';
                } else {
                    uploadStatus[name] = 'в очереди на обработку...';
                }
            });
            renderUploadStatus();
            if (settled) loadFolders();   // документ отнесён к папкам -> пересчитать «N док.»
        }

        function statusLabel(status) {
            switch (status) {
                case 'uploaded': return 'Загружен';
                case 'processing': return 'Обрабатывается (docling)...';
                case 'indexed': return 'Готов к поиску';
                case 'reanalyzing': return 'Переанализ…';
                case 'error': return 'Ошибка';
                default: return status;
            }
        }

        function formatSize(bytes) {
            if (!bytes && bytes !== 0) return '';
            const kb = bytes / 1024;
            return kb < 1024 ? `${kb.toFixed(0)} КБ` : `${(kb / 1024).toFixed(1)} МБ`;
        }

        let anyReanalyzing = false;   // есть документы в статусе reanalyzing -> опрашиваем чаще
        let docsCache = [];
        function loadDocuments() {
            return api('/documents').then(r => r.json()).then(d => {
                const docs = d.documents || [];
                docsCache = docs;
                anyReanalyzing = docs.some(doc => doc.status === 'reanalyzing');
                renderDocuments(docs);
                reconcilePending(docs);
            }).catch(() => {});
        }

        // ---------------- Смысловые папки (управляет человек) ----------------
        let allFolders = [];

        function loadFolders() {
            return api('/folders').then(r => r.json())
                .then(d => { allFolders = d.folders || []; renderFolders(); loadStageBoard(); }).catch(() => {});
        }

        // Форматы соответствуют config.SUPPORTED_EXT. Ключ — расширение файла (оно же mime в реестре).
        const BEXT = { pdf:'pdf', docx:'docx', doc:'docx', pptx:'ppt', html:'web', htm:'web', md:'md', txt:'txt' };
        const BLABEL = { pdf:'PDF', docx:'DOCX', doc:'DOC', pptx:'PPTX', html:'HTML', htm:'HTM', md:'MD', txt:'TXT' };
        function bDocCard(d) {
            // Тип — из mime ИЛИ из расширения имени файла (в реестре mime может быть пустым,
            // тогда раньше показывалось «ФАЙЛ»). docFormat даёт и то, и другое.
            const fmt = docFormat(d);
            const sc = (d.score != null) ? `<div class="sc">уверенность ${Number(d.score).toFixed(2)}</div>` : '';
            return `<div class="bdoc" data-fn="${escapeHtml(d.filename || '')}" onclick="openSubstageMap(this.dataset.fn)" title="Показать куски текста и критерий попадания"><div class="ext ${fmt.cls}">${fmt.label}</div><div><div class="nm">${escapeHtml(d.filename || '')}</div>${sc}</div></div>`;
        }
        function bCountDocs(s) { let n = (s.documents || []).length; (s.substages || []).forEach(x => n += (x.documents || []).length); return n; }
        function renderStageBoard(b) {
            const el = document.getElementById('stage-board');
            if (!el) return;
            let html = '';
            (b.stages || []).forEach((s, i) => {
                let rows = '';
                (s.substages || []).forEach(sub => {
                    rows += `<div class="sub-r"><div><div class="sub-t">${escapeHtml(sub.title || '')}</div><div class="sub-c">${(sub.documents || []).length} док.</div></div>`
                          + `<div class="sub-docs">${(sub.documents || []).length ? sub.documents.map(bDocCard).join('') : '<div class="bempty">— нет документов —</div>'}</div></div>`;
                });
                if ((s.documents || []).length) {
                    rows += `<div class="sub-r"><div><div class="sub-t">В этапе (без подэтапа)</div></div><div class="sub-docs">${s.documents.map(bDocCard).join('')}</div></div>`;
                }
                html += `<section class="stg"><header class="stg-head"><div class="stg-n">${i + 1}</div>`
                      + `<div><div class="stg-tt">${escapeHtml(s.title || '')}</div><div class="stg-dd">${escapeHtml(s.description || '')}</div></div>`
                      + `<span class="stg-count">${bCountDocs(s)} док.</span></header>${rows || '<div class="sub-r"><div class="bempty">нет подэтапов</div></div>'}</section>`;
            });
            if ((b.unassigned || []).length) {
                html += `<section class="stg unassigned"><header class="stg-head"><div class="stg-n">?</div>`
                      + `<div><div class="stg-tt">Без уверенной привязки</div><div class="stg-dd">Загружены, но не отнесены к этапу — проверьте вручную</div></div>`
                      + `<span class="stg-count">${b.unassigned.length} док.</span></header>`
                      + `<div class="sub-r"><div><div class="sub-t">Требует решения</div></div><div class="sub-docs">${b.unassigned.map(bDocCard).join('')}</div></div></section>`;
            }
            el.innerHTML = html || '<div class="empty-hint">Пока нет ни этапов, ни документов.</div>';
            refreshIcons();
        }
        // ---------------- Тексты плана адаптации ----------------
        let _planTextsLoaded = false;
        function ensurePlanTextsLoaded() {
            if (_planTextsLoaded) return;
            api('/plans').then(r => r.json()).then(d => {
                const sel = document.getElementById('pt-plan');
                const plans = d.plans || [];
                sel.innerHTML = plans.length
                    ? plans.map(p => `<option value="${escapeHtml(p.plan_id)}">${escapeHtml(p.title)}${p.role ? ' · ' + escapeHtml(p.role) : ''}</option>`).join('')
                    : '<option value="">— планов нет —</option>';
                _planTextsLoaded = true;               // фиксируем только после успешной загрузки
                if (plans.length) loadPlanTexts();
            }).catch(() => {
                // транзиентный сбой — не запираем вкладку, дадим повторить при следующем открытии
                document.getElementById('pt-body').innerHTML =
                    '<div class="empty-hint">Не удалось загрузить список планов. Нажмите «Обновить».</div>';
            });
        }
        function refreshPlanTexts() { _planTextsLoaded = false; ensurePlanTextsLoaded(); }
        function loadPlanTexts() {
            const pid = document.getElementById('pt-plan').value;
            if (!pid) { document.getElementById('pt-body').innerHTML = '<div class="empty-hint">Выберите план.</div>'; return; }
            // список должностей, под которые есть отдельные тексты (+ «общий»)
            api(`/plans/${encodeURIComponent(pid)}/professions`).then(r => r.ok ? r.json() : { professions: [] })
                .then(d => {
                    const profs = d.professions || [];
                    document.getElementById('pt-prof').innerHTML =
                        '<option value="">Общий текст</option>' +
                        profs.map(p => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join('');
                }).finally(renderPlanTexts);
        }
        function renderPlanTexts() {
            const pid = document.getElementById('pt-plan').value;
            const prof = document.getElementById('pt-prof').value || '';
            const body = document.getElementById('pt-body');
            const meta = document.getElementById('pt-meta');
            if (!pid) return;
            body.innerHTML = '<div class="empty-hint">Загрузка…</div>';
            api(`/plans/${encodeURIComponent(pid)}/schedule${prof ? '?profession=' + encodeURIComponent(prof) : ''}`)
                .then(r => r.status === 404 ? null : r.json())
                .then(sch => {
                    if (!sch) { meta.textContent = ''; body.innerHTML = '<div class="empty-hint">Тексты ещё не сгенерированы. Сгенерируйте план во вкладке «Планы».</div>'; return; }
                    const msgs = sch.messages || [];
                    meta.textContent = `сообщений: ${msgs.length}${sch.generated_at ? ' · ' + sch.generated_at.replace('T', ' ') : ''}`;
                    // группировка по этапам -> подэтапам (порядок как в расписании)
                    const stages = [];
                    const byStage = {};
                    msgs.forEach(m => {
                        const sid = (m.stage || {}).id;
                        if (!byStage[sid]) { byStage[sid] = { title: (m.stage || {}).title, order: (m.stage || {}).order || 0, subs: [] }; stages.push(sid); }
                        byStage[sid].subs.push(m);
                    });
                    stages.sort((a, b) => byStage[a].order - byStage[b].order);
                    body.innerHTML = stages.map((sid, i) => {
                        const st = byStage[sid];
                        const subs = st.subs.map(m => {
                            const txt = ((m.content || {}).text || '').trim();
                            const kind = (m.substage || {}).kind || '';
                            const st2 = m.status && m.status !== 'generated' ? ` <span class="pt-status">${escapeHtml(m.status)}</span>` : '';
                            const src = (m.sources || []).length ? `<div class="pt-src">Источники: ${m.sources.map(s => escapeHtml(typeof s === 'string' ? s : (s.filename || s.title || ''))).filter(Boolean).join(', ')}</div>` : '';
                            return `<div class="pt-sub">
                                <div class="pt-sub-h"><b>${escapeHtml((m.substage || {}).title || '')}</b>${kind ? ` <span class="pt-kind">${escapeHtml(kind)}</span>` : ''}${st2}</div>
                                <div class="pt-text">${txt ? escapeHtml(txt) : '<span class="empty-hint">— текст пуст —</span>'}</div>
                                ${src}
                            </div>`;
                        }).join('');
                        return `<section class="pt-stage"><h3 class="pt-stage-h">${i + 1}. ${escapeHtml(st.title || '')}</h3>${subs}</section>`;
                    }).join('') || '<div class="empty-hint">В расписании нет сообщений.</div>';
                    refreshIcons();
                })
                .catch(() => { body.innerHTML = '<div class="empty-hint">Не удалось загрузить тексты.</div>'; });
        }

        function loadStageBoard() {
            const el = document.getElementById('stage-board');
            if (!el) return;
            api('/documents/board').then(r => r.ok ? r.json() : null).then(b => {
                if (!b) { el.innerHTML = '<div class="empty-hint">Нет данных.</div>'; return; }
                renderStageBoard(b);
            }).catch(() => { el.innerHTML = '<div class="empty-hint">Не удалось загрузить структуру.</div>'; });
        }

        function smapClass(s) { return s >= 0.6 ? 'hi' : (s >= 0.5 ? 'mid' : 'lo'); }
        function openSubstageMap(filename) {
            if (!filename) return;
            const dlg = document.getElementById('substage-map-dialog');
            document.getElementById('substage-map-title').textContent = 'Куски документа: ' + filename;
            const body = document.getElementById('substage-map-body');
            body.innerHTML = '<div class="empty-hint">Загрузка…</div>';
            dlg.showModal();
            // Разметка документа по подэтапам — только LLM (docpipe): подэтапы с уверенностью,
            // обоснованием и пометкой «общая информация». Косинусной оценки больше нет.
            api(`/documents/${encodeURIComponent(filename)}/labels`)
                .then(r => r.status === 404 ? null : (r.ok ? r.json() : Promise.reject(new Error('labels'))))
                .then(d => {
                    if (d) { renderDocpipeMap(body, d, filename); return; }
                    body.innerHTML = '<div class="empty-hint">Документ ещё не размечен LLM — разметка идёт в фоне после загрузки. Обновите через минуту.</div>';
                })
                .catch(() => { body.innerHTML = '<div class="empty-hint">Ошибка загрузки.</div>'; });
        }

        function renderDocpipeMap(body, d, filename) {
            const secs = d.sections || [];
            const note = `<div class="plan-note" style="margin-bottom:10px;"><i data-lucide="scan-text"></i>
                Точная разметка (LLM, temp=0). Подробнее — с векторами и чанками — на странице
                <a href="/doc-breakdown">Разбор документа</a>.</div>`;
            if (!secs.length) { body.innerHTML = note + '<div class="empty-hint">Документ размечен, но блоков нет.</div>'; refreshIcons(); return; }
            body.innerHTML = note + secs.map((s, i) => {
                const head = `Кусок ${i + 1}${(s.heading_path && s.heading_path.length) ? ' · ' + escapeHtml(s.heading_path.join(' / ')) : ''}${s.page != null ? ' · стр. ' + s.page : ''}`;
                if (s.is_meaningful === false) {
                    return `<div class="smap-chunk smap-junk"><div class="smap-sec">${head} · служебный текст (в подэтапы не идёт)</div>`
                         + `<div class="txt">${escapeHtml((s.text || '').slice(0, 400))}</div></div>`;
                }
                let chips;
                if (s.is_general) {
                    chips = `<span class="smap-chip hi">общий для всех</span>`;
                } else {
                    chips = (s.substages || []).map(su =>
                        `<span class="smap-chip ${smapClass(su.confidence)}" title="${escapeHtml(su.description || '')}">`
                        + `${escapeHtml((s.stages[0] && s.stages[0].title) || '')} → ${escapeHtml(su.title || su.id)} `
                        + `<small>${su.confidence != null ? Math.round(su.confidence * 100) + '%' : ''}</small></span>`).join('');
                }
                const subs = chips ? `<div class="smap-subs">${chips}</div>` : '<div class="smap-none">— ни одному подэтапу не соответствует —</div>';
                const prof = s.is_general ? '' : ((s.professions || []).length ? `<div class="smap-sec">Профессии: ${escapeHtml(s.professions.join(', '))}</div>` : '');
                const why = s.why ? `<div class="smap-sec" style="color:#1e40af;">Почему: ${escapeHtml(s.why)}</div>` : '';
                return `<div class="smap-chunk"><div class="smap-sec">${head}</div>`
                     + `<div class="txt">${escapeHtml((s.text || '').slice(0, 600))}</div>${subs}${prof}${why}</div>`;
            }).join('');
            refreshIcons();
        }

        function renderFolders() {
            if (!folderGrid) return;   // сетка папок убрана из UI — рендерить некуда
            if (!allFolders.length) {
                folderGrid.innerHTML = `<div class="empty-hint">Папок пока нет — создайте первую.</div>`;
                return;
            }
            folderGrid.innerHTML = allFolders.map(f => `
                <div class="folder-card ${f.enabled ? '' : 'disabled'}">
                    <div class="name"><i data-lucide="folder"></i> ${escapeHtml(f.name)}${f.enabled ? '' : ' <span class="badge">выкл.</span>'}</div>
                    ${f.description ? `<div class="desc">${escapeHtml(f.description)}</div>` : ''}
                    <div class="count">${f.criteria.length} критериев · ${f.documents || 0} док.</div>
                    <div class="row-actions">
                        <button class="icon-btn" title="Показать чанки" onclick="showFolderChunks('${escapeHtml(f.slug)}','${escapeHtml(f.name)}')"><i data-lucide="layers"></i></button>
                        <button class="icon-btn" title="Изменить" onclick="openFolderDialog('${f.id}')"><i data-lucide="pencil"></i></button>
                        <button class="icon-btn" title="${f.enabled ? 'Отключить' : 'Включить'}" onclick="toggleFolder('${f.id}',${!f.enabled})"><i data-lucide="${f.enabled ? 'eye-off' : 'eye'}"></i></button>
                        <button class="icon-btn danger" title="Удалить" onclick="deleteFolder('${f.id}')"><i data-lucide="trash-2"></i></button>
                    </div>
                </div>
            `).join('');
            refreshIcons();
        }

        function critRow(val = '') {
            const div = document.createElement('div');
            div.className = 'crit-row';
            div.innerHTML = `<textarea placeholder="Признак, по которому документ относится к папке"></textarea>` +
                `<button class="icon-btn danger" onclick="this.parentElement.remove()"><i data-lucide="x"></i></button>`;
            div.querySelector('textarea').value = val;
            return div;
        }
        function addCriterion() { document.getElementById('folder-criteria').appendChild(critRow()); refreshIcons(); }

        function openFolderDialog(id) {
            const f = id ? allFolders.find(x => x.id === id) : null;
            document.getElementById('folder-dialog-title').textContent = f ? 'Изменить папку' : 'Новая папка';
            document.getElementById('folder-id').value = f ? f.id : '';
            document.getElementById('folder-name').value = f ? f.name : '';
            document.getElementById('folder-description').value = f ? f.description : '';
            const crit = document.getElementById('folder-criteria');
            crit.innerHTML = '';
            const list = f ? f.criteria : [];
            (list.length ? list : ['']).forEach(c => crit.appendChild(critRow(c)));
            document.getElementById('folder-error').style.display = 'none';
            document.getElementById('folder-dialog').showModal();
            refreshIcons();
        }

        function submitFolder() {
            const id = document.getElementById('folder-id').value;
            const name = document.getElementById('folder-name').value.trim();
            const err = document.getElementById('folder-error');
            if (!name) { err.textContent = 'Укажите название'; err.style.display = 'block'; return; }
            const criteria = [...document.querySelectorAll('#folder-criteria textarea')].map(t => t.value.trim()).filter(Boolean);
            const description = document.getElementById('folder-description').value.trim();
            const body = JSON.stringify({ name, description, criteria });
            const opts = { method: id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body };
            api(id ? '/folders/' + id : '/folders', opts)
                .then(r => r.json().then(d => ({ ok: r.ok, d })))
                .then(({ ok, d }) => {
                    if (!ok) { err.textContent = d.detail || 'Не удалось сохранить'; err.style.display = 'block'; return; }
                    document.getElementById('folder-dialog').close();
                    loadFolders();
                });
        }

        function toggleFolder(id, enabled) {
            api('/folders/' + id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }) })
                .then(() => loadFolders());
        }

        function deleteFolder(id) {
            const f = allFolders.find(x => x.id === id);
            if (!confirm(`Удалить папку «${f ? f.name : ''}»? Документы останутся в общей базе.`)) return;
            api('/folders/' + id, { method: 'DELETE' }).then(() => loadFolders());
        }

        // Суперадмин видит документы ВСЕХ администраторов и группирует их по владельцу
        // (как папки в хранилище: <суперадмин>/<администратор>/<файл>).
        // Обычный администратор получает с сервера только свои — группировки нет.
        function renderDocuments(docs) {
            if (!docs.length) {
                docList.innerHTML = `<div class="empty-hint">${isOwner
                    ? 'Пока никто из администраторов не загрузил документы.'
                    : 'Пока нет ваших документов — добавьте первый регламент выше.'}</div>`;
                return;
            }
            if (!isOwner) { docList.innerHTML = docs.map(docCard).join(''); refreshIcons(); return; }

            const groups = new Map();
            docs.forEach(d => {
                const key = d.uploaded_by_name || 'Без владельца (загружено до разделения прав)';
                if (!groups.has(key)) groups.set(key, []);
                groups.get(key).push(d);
            });
            docList.innerHTML = [...groups.entries()].map(([owner, list]) => {
                const dep = (list.find(d => d.department) || {}).department || '';
                return `<div class="owner-group">
                    <div class="owner-head"><i data-lucide="folder-open"></i>
                        <b>${escapeHtml(owner)}</b>${dep ? ` · ${escapeHtml(dep)}` : ''}
                        <span class="owner-count">${list.length} док.</span></div>
                    ${list.map(docCard).join('')}
                </div>`;
            }).join('');
            refreshIcons();
        }

        function docFormat(doc) {
            // Формат — из mime (реестр метаданных) или, если его нет, из расширения имени файла.
            const raw = (doc.mime || (doc.filename || '').split('.').pop() || '').toLowerCase();
            return { cls: BEXT[raw] || 'gen', label: BLABEL[raw] || (raw ? raw.toUpperCase().slice(0, 4) : 'ФАЙЛ') };
        }
        function docCard(doc) {
                const fmt = docFormat(doc);
                const meta = [
                    doc.size_bytes !== undefined ? formatSize(doc.size_bytes) : null,
                    doc.chunks ? `${doc.chunks} чанков` : null,
                    doc.uploaded_at ? doc.uploaded_at.replace('T', ' ') : null,
                ].filter(Boolean).join(' · ');
                const folderSlugs = doc.folders || [];
                const folderLine = folderSlugs.length
                    ? `<div class="chips">${folderSlugs.map(s => `<span class="folder-tag"><i data-lucide="folder"></i> ${escapeHtml(folderName(s))}</span>`).join('')}</div>`
                    : (doc.status === 'indexed' ? `<div class="doc-meta">Общая база «Все документы» (без папки)</div>` : '');
                const summaryLine = doc.summary
                    ? `<div class="doc-meta" style="color:#475569;margin-top:4px;">${escapeHtml(doc.summary.slice(0, 200))}${doc.summary.length > 200 ? '…' : ''}</div>` : '';
                const similar = (doc.similar || []);
                const similarLine = similar.length
                    ? `<div class="warn" style="margin-top:8px;"><i data-lucide="triangle-alert"></i> Похоже на: ${similar.map(s => escapeHtml(s.filename)).join(', ')} — возможен дубль/обновление. Удалите устаревший документ или дайте уточнение.</div>` : '';
                const clarLine = doc.clarification
                    ? `<div class="doc-meta" style="color:#0369a1;margin-top:4px;"><i data-lucide="info"></i> Уточнение: ${escapeHtml(doc.clarification)}</div>` : '';
                const errorLine = doc.status === 'error' && doc.error
                    ? `<div class="doc-meta" style="color:#dc2626;">${escapeHtml(doc.error)}</div>` : '';
                const ownerLine = doc.uploaded_by_name
                    ? `<div class="doc-meta"><i data-lucide="user"></i> Загрузил: ${escapeHtml(doc.uploaded_by_name)}${doc.department ? ' · ' + escapeHtml(doc.department) : ''}${doc.storage_path ? ` · <code>${escapeHtml(doc.storage_path)}</code>` : ''}</div>`
                    : '';
                return `
                    <div class="doc-item" style="flex-direction:column;align-items:stretch;">
                      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;">
                        <div class="doc-info">
                            <div class="doc-name" title="${escapeHtml(doc.filename)}"><span class="fmt-tag ${fmt.cls}">${fmt.label}</span> ${escapeHtml(doc.filename)}</div>
                            ${folderLine}
                            ${summaryLine}
                            <div class="doc-meta">${meta}</div>
                            ${ownerLine}
                            ${clarLine}
                            ${errorLine}
                        </div>
                        <div class="doc-status">
                            <span class="status-badge ${doc.status}">${statusLabel(doc.status)}</span>
                            ${doc.chunks ? `<button class="icon-btn" title="Подробнее" onclick="showDocDetail('${escapeHtml(doc.filename)}')"><i data-lucide="search"></i></button>` : ''}
                            ${doc.chunks ? `<button class="icon-btn" title="Переанализировать" onclick="reanalyzeDoc('${escapeHtml(doc.filename)}')"><i data-lucide="refresh-cw"></i></button>` : ''}
                            <button class="del-btn" onclick="deleteDocument('${escapeHtml(doc.filename)}')">Удалить</button>
                        </div>
                      </div>
                      ${similarLine}
                      ${similar.length ? `<div style="display:flex;gap:8px;margin-top:8px;">
                            <input type="text" id="clar-${escapeHtml(doc.filename)}" placeholder="Уточнение для ассистента (напр.: старый документ неактуален)" style="flex:1;">
                            <button class="ghost-btn" onclick="clarifyDoc('${escapeHtml(doc.filename)}')"><i data-lucide="save"></i> Сохранить</button>
                        </div>` : ''}
                    </div>
                `;
        }

        function folderName(slug) { const f = allFolders.find(x => x.slug === slug); return f ? f.name : slug; }

        function reanalyzeDoc(filename) {
            api('/documents/' + encodeURIComponent(filename) + '/reanalyze', { method: 'POST' })
                .then(() => { anyReanalyzing = true; bumpDocPolling(); });   // статус подтянется опросом
        }

        function clarifyDoc(filename) {
            const val = (document.getElementById('clar-' + filename) || {}).value || '';
            if (!val.trim()) return;
            api('/documents/' + encodeURIComponent(filename) + '/clarify', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ clarification: val.trim() })
            }).then(() => loadDocuments());
        }

        function reanalyzeAll() {
            if (!confirm('Запустить повторный анализ всей базы под текущую структуру папок?')) return;
            api('/documents/reanalyze', { method: 'POST' })
                .then(() => { anyReanalyzing = true; bumpDocPolling(); });   // статусы «Переанализ…» подтянутся опросом
        }

        function assignChunksToStages() {
            if (!confirm('Разложить все чанки по этапам адаптации? Нужно после загрузки документов, чтобы генерация плана брала чанки нужного этапа.')) return;
            setStatus('Раскладываю чанки по этапам…');
            api('/chunks/assign-stages', { method: 'POST' })
                .then(() => setStatus('Раскладка чанков по этапам запущена (идёт в фоне)'));
        }

        // ---------------- Штатное расписание ----------------
        const STAFFING_LABELS = { full_name: 'ФИО', position: 'Должность', department: 'Отдел', start_date: 'Дата приёма/выхода' };
        const STAFFING_KEYS = ['full_name', 'position', 'department', 'start_date'];
        let staffingRecords = [];

        function staffingPreview(fileArg) {
            const file = fileArg || document.getElementById('staffing-file').files[0];
            const status = document.getElementById('staffing-status');
            const preview = document.getElementById('staffing-preview');
            const result = document.getElementById('staffing-result');
            result.innerHTML = ''; preview.innerHTML = '';
            if (!file) { status.textContent = 'Выберите файл xlsx.'; return; }
            status.textContent = `ИИ разбирает «${file.name}»…`;
            const fd = new FormData(); fd.append('file', file);
            apiJson('/staffing/preview', { method: 'POST', body: fd })
                .then(({ ok, data }) => {
                    if (!ok) { status.innerHTML = `<span class="err">${escapeHtml(data.detail || 'ошибка разбора')}</span>`; return; }
                    staffingRecords = data.records || [];
                    const withName = staffingRecords.filter(r => (r.full_name || '').trim()).length;
                    status.textContent = `Найдено строк: ${data.count} (с ФИО: ${withName}, вакансий: ${data.count - withName}). Поля можно отредактировать.`;
                    window._staffingCols = (data.mapping || {}).columns || {};
                    renderStaffingPreview(window._staffingCols);
                })
                .catch(err => { status.innerHTML = `<span class="err">${escapeHtml(err.message)}</span>`; });
        }

        function renderStaffingPreview(cols) {
            const preview = document.getElementById('staffing-preview');
            if (!staffingRecords.length) {
                preview.innerHTML = '<div class="warn"><i data-lucide="triangle-alert"></i> В таблице не найдено данных. Проверьте файл или добавьте строки вручную.</div>';
                refreshIcons();
                return;
            }
            const head = STAFFING_KEYS.map(f => `<th>${STAFFING_LABELS[f]}</th>`).join('') + '<th></th>';
            const rows = staffingRecords.map((r, i) =>
                `<tr>${STAFFING_KEYS.map(f =>
                    `<td><input type="text" style="width:100%;box-sizing:border-box;" value="${escapeHtml(r[f] || '')}"
                          oninput="staffingRecords[${i}]['${f}']=this.value"></td>`).join('')}
                 <td><button class="icon-btn danger" title="Убрать строку" onclick="staffingRemoveRow(${i})"><i data-lucide="x"></i></button></td></tr>`).join('');
            preview.innerHTML = `
                <div class="section-head-row" style="margin:14px 0 6px;">
                    <div class="doc-meta muted">Строки с ФИО станут профилями, без ФИО — вакансиями.</div>
                    <button class="ghost-btn" onclick="staffingImport()"><i data-lucide="user-plus"></i> Создать (${staffingRecords.length})</button>
                </div>
                <div style="overflow-x:auto;"><table class="tst" style="font-size:13px;">
                    <thead><tr>${head}</tr></thead><tbody id="staffing-tbody">${rows}</tbody></table></div>`;
            refreshIcons();
        }

        function staffingRemoveRow(i) {
            staffingRecords.splice(i, 1);
            renderStaffingPreview(window._staffingCols || {});
        }

        function staffingImport() {
            if (!staffingRecords.length) return;
            if (!confirm(`Создать ${staffingRecords.length} записей? Строки с ФИО — профили (существующие по ФИО пропускаются), без ФИО — вакансии.`)) return;
            const status = document.getElementById('staffing-status');
            status.textContent = 'Создаём…';
            apiJson('/staffing/import', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ records: staffingRecords })
            }).then(({ ok, data }) => {
                if (!ok) { status.innerHTML = `<span class="err">${escapeHtml(data.detail || 'ошибка создания')}</span>`; return; }
                const profiles = data.profiles || [], vacancies = data.vacancies || [], skipped = data.skipped || [];
                status.textContent = `Профилей: ${profiles.length}, вакансий: ${vacancies.length}, пропущено: ${skipped.length}.`;
                window._staffingCreated = profiles;
                const pRows = profiles.map(c =>
                    `<tr><td>${escapeHtml(c.full_name)}</td><td class="mono">${escapeHtml(c.username)}</td><td class="mono">${escapeHtml(c.password)}</td><td>${escapeHtml(c.position || '')}</td></tr>`).join('');
                const vRows = vacancies.map(v => `<tr><td>${escapeHtml(v.position)}</td><td>${escapeHtml(v.department || '')}</td></tr>`).join('');
                const skipRows = skipped.map(s => `<div class="muted">${escapeHtml(s.full_name)} — ${escapeHtml(s.reason)}</div>`).join('');
                document.getElementById('staffing-result').innerHTML = `
                    ${profiles.length ? `<div class="warn" style="margin:12px 0;"><i data-lucide="triangle-alert"></i> Пароли показываются один раз — выгрузите их сейчас.</div>
                    <button class="ghost-btn" onclick="downloadStaffingCsv()"><i data-lucide="download"></i> Скачать логины и пароли (CSV)</button>
                    <div style="overflow-x:auto;margin-top:10px;"><table class="tst" style="font-size:13px;">
                        <thead><tr><th>ФИО</th><th>Логин</th><th>Пароль</th><th>Должность</th></tr></thead><tbody>${pRows}</tbody></table></div>` : ''}
                    ${vacancies.length ? `<div style="margin-top:14px;font-weight:600;">Вакансии (${vacancies.length}):</div>
                    <div style="overflow-x:auto;margin-top:6px;"><table class="tst" style="font-size:13px;">
                        <thead><tr><th>Должность</th><th>Отдел</th></tr></thead><tbody>${vRows}</tbody></table></div>` : ''}
                    ${skipRows ? `<div style="margin-top:12px;"><strong>Пропущены:</strong>${skipRows}</div>` : ''}`;
                refreshIcons();
                employeesLoaded = false;
                loadEmployees();
            }).catch(err => { status.innerHTML = `<span class="err">${escapeHtml(err.message)}</span>`; });
        }

        // Dropzone штатки: клик выбирает файл, drag-n-drop роняет файл — сразу разбираем.
        (function () {
            const dz = document.getElementById('staffing-dropzone');
            const inp = document.getElementById('staffing-file');
            if (!dz || !inp) return;
            dz.addEventListener('click', () => inp.click());
            inp.addEventListener('change', () => { if (inp.files[0]) staffingPreview(inp.files[0]); });
            ['dragenter', 'dragover'].forEach(evt => dz.addEventListener(evt, e => { e.preventDefault(); dz.classList.add('dragover'); }));
            ['dragleave', 'drop'].forEach(evt => dz.addEventListener(evt, e => { e.preventDefault(); dz.classList.remove('dragover'); }));
            dz.addEventListener('drop', e => { const f = e.dataTransfer.files[0]; if (f) staffingPreview(f); });
        })();

        function downloadStaffingCsv() {
            const created = window._staffingCreated || [];
            if (!created.length) return;
            const esc = v => `"${String(v == null ? '' : v).replace(/"/g, '""')}"`;
            const lines = [['ФИО', 'Логин', 'Пароль', 'Должность'].join(';')]
                .concat(created.map(c => [c.full_name, c.username, c.password, c.position || ''].map(esc).join(';')));
            const blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'staffing_credentials.csv';
            a.click();
            URL.revokeObjectURL(a.href);
        }

        function showDocDetail(filename) {
            const dialog = document.getElementById('doc-detail-dialog');
            const body = document.getElementById('doc-detail-body');
            document.getElementById('doc-detail-title').textContent = `${filename}`;
            body.innerHTML = `<div class="empty-hint">Загружаем чанки и векторы…</div>`;
            dialog.showModal();
            apiJson(`/documents/${encodeURIComponent(filename)}/chunks`)
                .then(({ ok, data }) => {
                    if (!ok) { body.innerHTML = `<div class="warn"><i data-lucide="triangle-alert"></i> ${escapeHtml(data.detail || 'Не удалось получить данные')}</div>`; return; }
                    body.innerHTML = renderDocDetail(data);
                })
                .catch(err => { body.innerHTML = `<div class="error">Ошибка: ${escapeHtml(err.message)}</div>`; });
        }

        function showFolderChunks(slug, name) {
            const dialog = document.getElementById('doc-detail-dialog');
            const body = document.getElementById('doc-detail-body');
            document.getElementById('doc-detail-title').textContent = `Папка: ${name}`;
            body.innerHTML = `<div class="empty-hint">Загружаем чанки папки…</div>`;
            dialog.showModal();
            apiJson(`/folders/${encodeURIComponent(slug)}/chunks`)
                .then(({ ok, data }) => {
                    if (!ok) { body.innerHTML = `<div class="warn"><i data-lucide="triangle-alert"></i> ${escapeHtml(data.detail || 'Не удалось получить данные')}</div>`; return; }
                    body.innerHTML = renderFolderChunks(data);
                    refreshIcons();
                })
                .catch(err => { body.innerHTML = `<div class="error">Ошибка: ${escapeHtml(err.message)}</div>`; });
        }

        function renderFolderChunks(data) {
            const chunks = data.chunks || [];
            if (!chunks.length) return `<div class="empty-hint">В этой папке пока нет чанков. Загрузите/переанализируйте документы под текущую структуру папок.</div>`;
            const head = `<div class="doc-meta" style="margin-bottom:12px;">
                Чанков в папке: <strong>${data.count}</strong>${data.truncated ? ' (показаны первые)' : ''}.
                Это конкретные фрагменты документов, отнесённые к папке по смыслу — именно из них
                формируется ответ на вопрос в этой теме.
            </div>`;
            const rows = chunks.map(c => {
                const meta = [
                    c.section ? `§ ${escapeHtml(c.section)}` : null,
                    c.page != null ? `стр. ${c.page}` : null,
                    c.length != null ? `${c.length} симв.` : null,
                ].filter(Boolean).join(' · ');
                return `<div class="card" style="margin-bottom:10px;">
                    <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;">
                        <strong><i data-lucide="file-text"></i> ${escapeHtml(c.source || '—')} · чанк #${c.chunk_index != null ? c.chunk_index : '?'}</strong>
                        <small style="color:#94a3b8;">id: ${escapeHtml(c.id)}</small>
                    </div>
                    ${meta ? `<div class="doc-meta" style="margin:4px 0;">${meta}</div>` : ''}
                    <div class="msg-text" style="white-space:pre-wrap;margin-top:6px;">${escapeHtml(c.text || '—')}</div>
                </div>`;
            }).join('');
            return head + rows;
        }

        function renderDocDetail(data) {
            const chunks = data.chunks || [];
            const dim = chunks.length ? chunks[0].vector.dim : 0;
            const head = `<div class="doc-meta" style="margin-bottom:12px;">
                Всего чанков: <strong>${chunks.length}</strong> ·
                размерность вектора: <strong>${dim}</strong> ·
                модель эмбеддинга: <strong>bge-m3</strong>.
                Каждый чанк — отдельная точка в Qdrant: слева текст, ушедший в эмбеддинг,
                справа — вектор этого текста (норма и первые 16 из ${dim} значений).
            </div>`;
            const rows = chunks.map(c => {
                const headings = (c.headings || []).filter(Boolean).join(' / ');
                const meta = [
                    c.section ? `§ ${escapeHtml(c.section)}` : null,
                    c.page != null ? `стр. ${c.page}` : null,
                    c.length != null ? `${c.length} симв.` : null,
                    `папки: ${(c.folders && c.folders.length) ? c.folders.map(s => escapeHtml(folderName(s))).join(', ') : '— (общая база)'}`,
                ].filter(Boolean).join(' · ');
                const vec = c.vector || { dim: 0, norm: 0, preview: [] };
                const preview = (vec.preview || []).map(v => v.toFixed(4)).join(', ');
                return `<div class="card" style="margin-bottom:10px;">
                    <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;">
                        <strong>Чанк #${c.chunk_index != null ? c.chunk_index : '?'}</strong>
                        <small style="color:#94a3b8;">id: ${escapeHtml(c.id)}</small>
                    </div>
                    <div class="doc-meta" style="margin:4px 0;">${meta}</div>
                    ${headings ? `<div class="doc-meta" style="color:#475569;"><i data-lucide="milestone"></i> ${escapeHtml(headings)}</div>` : ''}
                    <div class="chunk-detail-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px;">
                        <div>
                            <div class="doc-meta" style="margin-bottom:4px;">Текст (ушёл в эмбеддинг):</div>
                            <div class="msg-text" style="white-space:pre-wrap;">${escapeHtml(c.text || '—')}</div>
                        </div>
                        <div>
                            <div class="doc-meta" style="margin-bottom:4px;">Вектор: dim=${vec.dim}, ‖v‖=${vec.norm}</div>
                            <code style="display:block;background:#0f172a;color:#a5b4fc;padding:8px;border-radius:6px;font-size:12px;white-space:pre-wrap;word-break:break-all;">[${preview}${vec.dim > (vec.preview || []).length ? ', …' : ''}]</code>
                        </div>
                    </div>
                </div>`;
            }).join('');
            return head + (rows || `<div class="empty-hint">Чанков нет.</div>`);
        }

        function deleteDocument(filename) {
            if (!confirm(`Удалить документ "${filename}" и все его данные из индекса?`)) return;
            api(`/documents/${encodeURIComponent(filename)}`, { method: 'DELETE' })
                .then(() => { loadDocuments(); loadFolders(); })
                .catch(err => alert(`Ошибка удаления: ${err.message}`));
        }

        // Самопланирующийся опрос: чаще, пока есть незавершённые загрузки; иначе редко.
        let docPollTimer = null;
        function scheduleDocPoll() {
            clearTimeout(docPollTimer);
            docPollTimer = setTimeout(() => {
                loadDocuments().finally(scheduleDocPoll);
            }, (pendingDocs.size || anyReanalyzing) ? 2000 : 8000);
        }
        function bumpDocPolling() { scheduleDocPoll(); }   // ускорить сразу после загрузки файла

        loadDocuments();
        loadFolders();
        scheduleDocPoll();

        // ---------------- Конструктор плана ----------------
        let catalog = null;
        let plan = emptyPlan();
        let builderLoaded = false;
        let pollTimer = null;
        let currentGenJob = null;

        function emptyPlan() {
            return { plan_id: null, title: '', role: '', start_date: '', stages: [] };
        }

        function ensureBuilderLoaded() {
            if (builderLoaded) return;
            builderLoaded = true;
            Promise.all([
                api('/catalog').then(r => r.json()),
                api('/plans').then(r => r.json()),
            ]).then(([cat, plans]) => {
                catalog = cat;
                fillStageCatalogSelect();
                fillPlanSelect(plans.plans || []);
                renderStages();
            }).catch(err => {
                document.getElementById('stages-container').innerHTML =
                    `<div class="error">Не удалось загрузить каталог этапов: ${escapeHtml(err.message)}</div>`;
            });
        }

        function fillStageCatalogSelect() {
            const select = document.getElementById('stage-catalog-select');
            select.innerHTML = catalog.stages
                .map(s => `<option value="${s.id}">${escapeHtml(s.title)}</option>`).join('')
                + `<option value="__custom__">— свой этап —</option>`;
        }

        function fillPlanSelect(plans) {
            const select = document.getElementById('plan-select');
            const current = plan.plan_id || '';
            select.innerHTML = `<option value="">— выберите план —</option>` + plans.map(p =>
                `<option value="${escapeHtml(p.plan_id)}">${escapeHtml(p.title)}${p.role ? ' · ' + escapeHtml(p.role) : ''}</option>`
            ).join('');
            select.value = current;
        }

        function refreshPlanList() {
            return api('/plans').then(r => r.json()).then(d => {
                fillPlanSelect(d.plans || []);
                fillEmployeePlanSelect(d.plans || []);
            });
        }

        function onPlanSelect() {
            const id = document.getElementById('plan-select').value;
            if (!id) {
                // Создание нового плана отключено — просто очищаем редактор.
                plan = emptyPlan();
                document.getElementById('stages-container').innerHTML = '';
                document.getElementById('schedule-result').innerHTML = '';
                fillPlanMeta();
                setStatus('Выберите план для редактирования');
                return;
            }
            api(`/plans/${encodeURIComponent(id)}`).then(r => r.json()).then(data => {
                plan = data.plan;
                fillPlanMeta();
                renderStages();
                document.getElementById('schedule-result').innerHTML = '';
                loadScheduleIfAny();
            });
        }

        function newPlan() {
            plan = emptyPlan();
            document.getElementById('plan-select').value = '';
            document.getElementById('schedule-result').innerHTML = '';
            fillPlanMeta();
            renderStages();
            setStatus('');
        }

        function fullTemplatePlan() {
            const title = prompt('Название полного шаблона (единый для всех профессий):', 'Универсальный план адаптации');
            if (title === null) return;
            setStatus('Создаю полный шаблон…');
            api('/plans/template' + (title.trim() ? '?title=' + encodeURIComponent(title.trim()) : ''), { method: 'POST' })
                .then(r => r.json()).then(p => {
                    plan = p;
                    fillPlanMeta();
                    renderStages();
                    document.getElementById('schedule-result').innerHTML = '';
                    refreshPlanList().then(() => { document.getElementById('plan-select').value = p.plan_id; });
                    setStatus('Полный шаблон создан — редактируйте под задачу');
                });
        }

        function deletePlan() {
            if (!plan.plan_id) { newPlan(); return; }
            if (!confirm(`Удалить план «${plan.title}» вместе со сгенерированным расписанием?`)) return;
            api(`/plans/${encodeURIComponent(plan.plan_id)}`, { method: 'DELETE' })
                .then(() => { newPlan(); refreshPlanList(); });
        }

        function duplicatePlan() {
            if (!plan.plan_id) { setStatus('Сначала сохраните план, потом его можно дублировать'); return; }
            const title = prompt('Название копии (под смежную должность):', `${plan.title} (копия)`);
            if (title === null) return;  // отмена
            const q = title.trim() ? `?title=${encodeURIComponent(title.trim())}` : '';
            apiJson(`/plans/${encodeURIComponent(plan.plan_id)}/duplicate${q}`, { method: 'POST' })
                .then(({ ok, data }) => {
                    if (!ok) { setStatus(data.detail || 'Не удалось дублировать план'); return; }
                    plan = data;
                    fillPlanMeta();
                    renderStages();
                    refreshPlanList().then(() => {
                        document.getElementById('plan-select').value = data.plan_id;
                    });
                    setStatus('Создана копия — отредактируйте под должность и сохраните');
                });
        }

        function fillPlanMeta() {
            document.getElementById('plan-title').value = plan.title || '';
            document.getElementById('plan-role').value = plan.role || '';
            document.getElementById('plan-start').value = plan.start_date || '';
        }

        function planField(field, value) { plan[field] = value; }
        function setStatus(text) { document.getElementById('plan-status').textContent = text; }

        const UNIT_TITLES = { hours: 'часы', days: 'дни', weeks: 'недели', months: 'месяцы' };
        const UNIT_DAYS = { hours: 0, days: 1, weeks: 7, months: 30 };

        function stageSpanDays(duration) {
            const value = Math.max(1, parseInt(duration.value, 10) || 1);
            if (duration.unit === 'hours') return Math.max(1, Math.ceil(value / 24));
            return Math.max(1, value * (UNIT_DAYS[duration.unit] || 1));
        }

        function dayChoiceAllowed(duration) { return duration.unit !== 'hours'; }

        function computeOffsets() {
            const offsets = {};
            const before = plan.stages.filter(s => s.anchor === 'before_start');
            const after = plan.stages.filter(s => s.anchor !== 'before_start');
            let cursor = -before.reduce((sum, s) => sum + stageSpanDays(s.duration), 0);
            before.forEach(s => { offsets[s.uid] = cursor; cursor += stageSpanDays(s.duration); });
            cursor = 0;
            after.forEach(s => { offsets[s.uid] = cursor; cursor += stageSpanDays(s.duration); });
            return offsets;
        }

        function shiftDate(startDate, offsetDays) {
            const d = new Date(`${startDate}T00:00:00`);
            d.setDate(d.getDate() + offsetDays);
            return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
        }

        function sendDateLabel(offsetDays, time) {
            if (!plan.start_date) return `${offsetDays >= 0 ? '+' : ''}${offsetDays} дн. от даты выхода, ${time}`;
            return `${shiftDate(plan.start_date, offsetDays)} ${time}`;
        }

        let uidCounter = 0;
        function nextUid(prefix) { uidCounter += 1; return `${prefix}${uidCounter}`; }

        function addStageFromCatalog() {
            const id = document.getElementById('stage-catalog-select').value;
            if (id === '__custom__') {
                plan.stages.push({
                    uid: nextUid('u'), catalog_id: null, title: 'Новый этап', description: '',
                    anchor: 'from_start', duration: { value: 1, unit: 'days' }, substages: [],
                });
            } else {
                const template = catalog.stages.find(s => s.id === id);
                if (!template) return;
                plan.stages.push({
                    uid: nextUid('u'),
                    catalog_id: template.id,
                    title: template.title,
                    description: template.description,
                    anchor: template.anchor,
                    duration: { ...template.default_duration },
                    substages: [],
                });
            }
            renderStages();
        }

        function removeStage(index) { plan.stages.splice(index, 1); renderStages(); }

        function moveStage(index, delta) {
            const target = index + delta;
            if (target < 0 || target >= plan.stages.length) return;
            const [item] = plan.stages.splice(index, 1);
            plan.stages.splice(target, 0, item);
            renderStages();
        }

        function stageField(index, field, value) { plan.stages[index][field] = value; renderStages(); }

        function durationField(index, field, value) {
            plan.stages[index].duration[field] = field === 'value' ? (parseInt(value, 10) || 1) : value;
            const span = stageSpanDays(plan.stages[index].duration);
            plan.stages[index].substages.forEach(sub => {
                sub.schedule.day = Math.max(1, Math.min(span, sub.schedule.day || 1));
            });
            renderStages();
        }

        function substageTemplates(stage) {
            if (!stage.catalog_id || !catalog) return [];
            const found = catalog.stages.find(s => s.id === stage.catalog_id);
            return found ? found.substage_templates : [];
        }

        function addSubstage(stageIndex) {
            const stage = plan.stages[stageIndex];
            const select = document.getElementById(`sub-template-${stageIndex}`);
            const value = select ? select.value : '__custom__';
            const template = substageTemplates(stage).find(t => t.id === value);
            if (template) {
                stage.substages.push({
                    uid: nextUid('s'), catalog_id: template.id, title: template.title,
                    kind: template.kind, brief: template.brief, source: 'template',
                    tags: template.tags || [],
                    schedule: { day: 1, time: template.default_time || '09:00' },
                });
            } else {
                stage.substages.push({
                    uid: nextUid('s'), catalog_id: null, title: 'Новый подэтап',
                    kind: 'message', brief: '', source: 'manual', tags: [],
                    schedule: { day: 1, time: '09:00' },
                });
            }
            renderStages();
        }

        function removeSubstage(stageIndex, subIndex) {
            plan.stages[stageIndex].substages.splice(subIndex, 1);
            renderStages();
        }

        function moveSubstage(stageIndex, subIndex, delta) {
            const subs = plan.stages[stageIndex].substages;
            const target = subIndex + delta;
            if (target < 0 || target >= subs.length) return;
            const [item] = subs.splice(subIndex, 1);
            subs.splice(target, 0, item);
            renderStages();
        }

        function subField(stageIndex, subIndex, field, value) {
            const sub = plan.stages[stageIndex].substages[subIndex];
            if (field === 'brief' || field === 'title') {
                sub[field] = value;
                if (field === 'brief') sub.source = 'manual';
                return; // без перерисовки, чтобы не терять фокус ввода
            }
            sub[field] = value;
            renderStages();
        }

        function subSchedule(stageIndex, subIndex, field, value) {
            const sub = plan.stages[stageIndex].substages[subIndex];
            sub.schedule[field] = field === 'day' ? (parseInt(value, 10) || 1) : value;
            renderStages();
        }

        function renderStages() {
            const container = document.getElementById('stages-container');
            if (!plan.stages.length) {
                container.innerHTML = `<div class="empty-hint">Этапов пока нет. Выберите этап из списка ниже и добавьте его в план.</div>`;
                return;
            }
            plan.stages.forEach(s => { if (!s.uid) s.uid = nextUid('u'); });
            const offsets = computeOffsets();

            container.innerHTML = plan.stages.map((stage, stageIndex) => {
                const span = stageSpanDays(stage.duration);
                const dayChoice = dayChoiceAllowed(stage.duration);
                const offset = offsets[stage.uid] || 0;
                const templateOptions = substageTemplates(stage).map(t =>
                    `<option value="${t.id}">${escapeHtml(t.title)}</option>`).join('')
                    + `<option value="__custom__">— свой подэтап (текст вручную) —</option>`;

                const substagesHtml = stage.substages.map((sub, subIndex) => {
                    const dayOptions = Array.from({ length: span }, (_, i) =>
                        `<option value="${i + 1}" ${sub.schedule.day === i + 1 ? 'selected' : ''}>День ${i + 1}</option>`
                    ).join('');
                    const offsetDays = offset + (dayChoice ? (sub.schedule.day || 1) - 1 : 0);
                    const kindOptions = catalog.substage_kinds.map(k =>
                        `<option value="${k.id}" ${sub.kind === k.id ? 'selected' : ''}>${escapeHtml(k.title)}</option>`
                    ).join('');

                    return `
                        <div class="sub-item">
                            <div class="sub-head">
                                <span style="color:#94a3b8;">${subIndex + 1}.</span>
                                <input type="text" value="${escapeHtml(sub.title)}"
                                       oninput="subField(${stageIndex}, ${subIndex}, 'title', this.value)">
                                <button class="icon-btn" onclick="moveSubstage(${stageIndex}, ${subIndex}, -1)"><i data-lucide="chevron-up"></i></button>
                                <button class="icon-btn" onclick="moveSubstage(${stageIndex}, ${subIndex}, 1)"><i data-lucide="chevron-down"></i></button>
                                <button class="icon-btn danger" onclick="removeSubstage(${stageIndex}, ${subIndex})"><i data-lucide="x"></i></button>
                            </div>
                            <div style="margin-top:10px;">
                                <label style="font-size:12px;color:#64748b;">Что должен написать бот (основа для генерации по документам)</label>
                                <textarea oninput="subField(${stageIndex}, ${subIndex}, 'brief', this.value)"
                                          placeholder="Опишите, о чём сообщение. Текст можно взять из шаблона или написать свой.">${escapeHtml(sub.brief)}</textarea>
                            </div>
                            <div class="sub-grid">
                                <div class="field">
                                    <label>Тип</label>
                                    <select onchange="subField(${stageIndex}, ${subIndex}, 'kind', this.value)">${kindOptions}</select>
                                </div>
                                <div class="field">
                                    <label>День внутри этапа</label>
                                    <select ${dayChoice ? '' : 'disabled'}
                                            onchange="subSchedule(${stageIndex}, ${subIndex}, 'day', this.value)">
                                        ${dayChoice ? dayOptions : '<option>— этап задан в часах —</option>'}
                                    </select>
                                </div>
                                <div class="field">
                                    <label>Время</label>
                                    <input type="time" value="${escapeHtml(sub.schedule.time)}"
                                           onchange="subSchedule(${stageIndex}, ${subIndex}, 'time', this.value)">
                                </div>
                                <div class="sub-when"><i data-lucide="calendar-days"></i> ${escapeHtml(sendDateLabel(offsetDays, sub.schedule.time))}</div>
                            </div>
                        </div>
                    `;
                }).join('');

                const unitOptions = Object.entries(UNIT_TITLES).map(([id, title]) =>
                    `<option value="${id}" ${stage.duration.unit === id ? 'selected' : ''}>${title}</option>`).join('');

                return `
                    <div class="stage-card">
                        <div class="stage-head">
                            <div style="flex:1;">
                                <input type="text" class="stage-title-input" value="${escapeHtml(stage.title)}"
                                       oninput="plan.stages[${stageIndex}].title = this.value">
                            </div>
                            <div>
                                <button class="icon-btn" onclick="moveStage(${stageIndex}, -1)"><i data-lucide="chevron-up"></i></button>
                                <button class="icon-btn" onclick="moveStage(${stageIndex}, 1)"><i data-lucide="chevron-down"></i></button>
                                <button class="icon-btn danger" onclick="removeStage(${stageIndex})">Удалить этап</button>
                            </div>
                        </div>
                        ${stage.description ? `<div class="stage-hint">${escapeHtml(stage.description)}</div>` : ''}
                        <div class="stage-controls">
                            <div class="field">
                                <label>Длительность</label>
                                <input type="number" min="1" style="width:90px;" value="${stage.duration.value}"
                                       onchange="durationField(${stageIndex}, 'value', this.value)">
                            </div>
                            <div class="field">
                                <label>Единица</label>
                                <select onchange="durationField(${stageIndex}, 'unit', this.value)">${unitOptions}</select>
                            </div>
                            <div class="field">
                                <label>Отсчёт</label>
                                <select onchange="stageField(${stageIndex}, 'anchor', this.value)">
                                    <option value="from_start" ${stage.anchor !== 'before_start' ? 'selected' : ''}>от даты выхода</option>
                                    <option value="before_start" ${stage.anchor === 'before_start' ? 'selected' : ''}>до выхода на работу</option>
                                </select>
                            </div>
                            <div class="stage-hint">
                                ${span} кал. дн. · старт этапа ${offset >= 0 ? '+' : ''}${offset} дн. от даты выхода
                                ${dayChoice ? '' : ' · день не выбирается, только время'}
                            </div>
                        </div>

                        <div style="margin-top:14px;">${substagesHtml || '<div class="empty-hint">Подэтапов пока нет.</div>'}</div>

                        <div class="add-row">
                            <select id="sub-template-${stageIndex}">${templateOptions}</select>
                            <button class="ghost-btn" onclick="addSubstage(${stageIndex})">＋ Добавить подэтап</button>
                        </div>
                    </div>
                `;
            }).join('');
        }

        function planPayload() {
            return {
                title: plan.title || 'План адаптации',
                role: plan.role || '',
                start_date: plan.start_date || null,
                stages: plan.stages.map(stage => ({
                    id: stage.id || null,
                    catalog_id: stage.catalog_id,
                    title: stage.title,
                    description: stage.description || '',
                    anchor: stage.anchor,
                    duration: stage.duration,
                    substages: stage.substages.map(sub => ({
                        id: sub.id || null,
                        catalog_id: sub.catalog_id,
                        title: sub.title,
                        kind: sub.kind,
                        brief: sub.brief,
                        source: sub.source,
                        tags: sub.tags || [],
                        schedule: sub.schedule,
                    })),
                })),
            };
        }

        function savePlan() {
            // Создание нового плана отключено — редактируем только выбранный существующий.
            if (!plan.plan_id) {
                setStatus('Выберите план для редактирования — создание нового отключено');
                return Promise.reject(new Error('план не выбран'));
            }
            setStatus('Сохранение...');
            return api(`/plans/${encodeURIComponent(plan.plan_id)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(planPayload()),
            })
                .then(res => res.json())
                .then(saved => {
                    const uids = plan.stages.map(s => ({ uid: s.uid, subs: s.substages.map(x => x.uid) }));
                    plan = saved;
                    plan.stages.forEach((stage, i) => {
                        stage.uid = (uids[i] || {}).uid || nextUid('u');
                        stage.substages.forEach((sub, j) => {
                            sub.uid = ((uids[i] || {}).subs || [])[j] || nextUid('s');
                        });
                    });
                    fillPlanMeta();
                    renderStages();
                    setStatus(`Сохранено: ${saved.title}`);
                    return refreshPlanList().then(() => {
                        document.getElementById('plan-select').value = saved.plan_id;
                    });
                })
                .catch(err => setStatus(`Ошибка сохранения: ${err.message}`));
        }

        function generatePlan() {
            if (!plan.stages.some(s => s.substages.length)) {
                setStatus('Добавьте хотя бы один подэтап.');
                return;
            }
            document.getElementById('generate-btn').disabled = true;
            savePlan().then(() =>
                apiJson(`/plans/${encodeURIComponent(plan.plan_id)}/generate`, { method: 'POST' })
            ).then(({ ok, data }) => {
                if (!ok) {
                    setStatus(`${data.detail || 'Не удалось запустить генерацию'}`);
                    document.getElementById('generate-btn').disabled = false;
                    return;
                }
                setStatus('Генерация запущена');
                currentGenJob = data.job_id;
                document.getElementById('cancel-gen-btn').style.display = '';
                pollJob(data.job_id);
            }).catch(err => {
                setStatus(`${err.message}`);
                document.getElementById('generate-btn').disabled = false;
            });
        }

        function pollJob(jobId) {
            const wrap = document.getElementById('progress-wrap');
            const fill = document.getElementById('progress-fill');
            const label = document.getElementById('progress-label');
            wrap.style.display = 'block';

            clearInterval(pollTimer);
            pollTimer = setInterval(() => {
                api(`/jobs/${encodeURIComponent(jobId)}`).then(r => r.json()).then(job => {
                    const percent = job.total ? Math.round(100 * job.done / job.total) : 0;
                    fill.style.width = `${percent}%`;
                    label.textContent = job.status === 'running'
                        ? `Подэтап ${job.done} из ${job.total}${job.current ? ' · ' + job.current : ''}${job.errors ? ' · ошибок: ' + job.errors : ''}`
                        : `Статус: ${job.status} · ${job.done} из ${job.total}${job.errors ? ' · ошибок: ' + job.errors : ''}`;

                    if (job.status === 'done' || job.status === 'error' || job.status === 'cancelled') {
                        clearInterval(pollTimer);
                        currentGenJob = null;
                        document.getElementById('generate-btn').disabled = false;
                        document.getElementById('cancel-gen-btn').style.display = 'none';
                        setStatus(job.status === 'done' ? 'Генерация завершена'
                            : job.status === 'cancelled' ? 'Генерация отменена (сгенерированное сохранено)'
                            : `${job.error}`);
                        loadScheduleIfAny();
                        refreshPlanList();
                    }
                }).catch(() => clearInterval(pollTimer));
            }, 1500);
        }

        function cancelGeneration() {
            if (!currentGenJob) return;
            const btn = document.getElementById('cancel-gen-btn');
            btn.disabled = true;
            setStatus('Отмена…');
            api(`/jobs/${encodeURIComponent(currentGenJob)}/cancel`, { method: 'POST' })
                .finally(() => { btn.disabled = false; });
        }

        function loadScheduleIfAny() {
            if (!plan.plan_id) return;
            api(`/plans/${encodeURIComponent(plan.plan_id)}/schedule`)
                .then(res => res.ok ? res.json() : null)
                .then(schedule => { if (schedule) renderSchedule(schedule); })
                .catch(() => {});
        }

        function whenLabel(msg) {
            if (msg.schedule.send_at) return msg.schedule.send_at.replace('T', ' ');
            const anchor = msg.schedule.anchor === 'before_start' ? 'до выхода' : 'от выхода';
            const offset = msg.schedule.offset_days;
            return `${offset >= 0 ? '+' : ''}${offset} дн. (${anchor}), ${msg.schedule.time}`;
        }

        function messageRows(messages, withRegenerate) {
            return (messages || []).map(msg => `
                <tr>
                    <td>${msg.stage.order}. ${escapeHtml(msg.stage.title)}</td>
                    <td>${msg.substage.order}. ${escapeHtml(msg.substage.title)}<div class="msg-meta">${escapeHtml(msg.substage.kind)}</div></td>
                    <td style="white-space:nowrap;">${escapeHtml(whenLabel(msg))}</td>
                    <td>
                        <div class="msg-text">${escapeHtml(msg.content.text || '—')}</div>
                        ${msg.error ? `<div class="msg-error"><i data-lucide="triangle-alert"></i> ${escapeHtml(msg.error)}</div>` : ''}
                        <div class="msg-meta">
                            ${msg.folders_used && msg.folders_used.length ? '' + escapeHtml(msg.folders_used.join(', ')) + ' · ' : ''}
                            ${msg.sources && msg.sources.length ? '' + escapeHtml([...new Set(msg.sources.map(s => s.source))].join(', ')) : ''}
                        </div>
                        ${withRegenerate ? `<button class="icon-btn" style="margin-top:8px;"
                                onclick="regenerate('${escapeHtml(msg.message_id)}')"><i data-lucide="refresh-cw"></i> Перегенерировать</button>` : ''}
                    </td>
                </tr>
            `).join('');
        }

        function renderSchedule(schedule) {
            const base = `/plans/${encodeURIComponent(schedule.plan_id)}/export`;
            document.getElementById('schedule-result').innerHTML = `
                <h2 class="section-title"><i data-lucide="upload"></i> Сгенерированное расписание шаблона</h2>
                <div class="export-links">
                    <a href="${base}/schedule.md" download><i data-lucide="download"></i> schedule.md — расписание с ответами</a>
                    <a href="${base}/schedule.json" download><i data-lucide="download"></i> schedule.json — для мессенджеров и приложения</a>
                    <a href="${base}/plan.md" download><i data-lucide="download"></i> plan.md — план в формате для LLM</a>
                    <a href="${base}/plan.json" download><i data-lucide="download"></i> plan.json — канонический план</a>
                </div>
                <table class="schedule">
                    <thead><tr><th>Этап</th><th>Подэтап</th><th>Дата и время</th><th>Ответ</th></tr></thead>
                    <tbody>${messageRows(schedule.messages, true)}</tbody>
                </table>
            `;
        }

        function regenerate(messageId) {
            setStatus(`Перегенерация: ${messageId}`);
            api(`/plans/${encodeURIComponent(plan.plan_id)}/messages/${encodeURIComponent(messageId)}/regenerate`,
                { method: 'POST' })
                .then(res => res.json())
                .then(() => { setStatus('Подэтап обновлён'); loadScheduleIfAny(); })
                .catch(err => setStatus(`${err.message}`));
        }

        // ---------------- Пользователи ----------------
        let employeesLoaded = false;
        let editingEmployeeId = null;
        let employeesCache = [];

        // Логин и пароль отправляются только при создании: у существующего пользователя
        // они меняются отдельной ручкой /users/{id}/credentials
        const PROFILE_FIELDS = ['full_name', 'position', 'department', 'contact',
                                'mentor', 'manager', 'plan_id', 'start_date', 'status', 'notes'];
        const CREDENTIAL_FIELDS = ['username', 'password'];
        const EMPLOYEE_STATUS_TITLES = {
            planned: 'Запланирован', active: 'Проходит адаптацию',
            paused: 'Приостановлен', done: 'Завершил',
        };
        const ROLE_TITLES = { owner: 'Суперадмин', admin: 'Администратор', employee: 'Сотрудник' };

        function ensureEmployeesLoaded() {
            if (employeesLoaded) return;
            employeesLoaded = true;
            api('/plans').then(r => r.json()).then(d => fillEmployeePlanSelect(d.plans || []));
            loadEmployees();
        }

        function fillEmployeePlanSelect(plans) {
            const select = document.getElementById('emp-plan_id');
            const current = select.value;
            select.innerHTML = `<option value="">— план не назначен —</option>` + plans.map(p =>
                `<option value="${escapeHtml(p.plan_id)}">${escapeHtml(p.title)}${p.generated ? ' <i data-lucide="check"></i>' : ' (без ответов)'}</option>`
            ).join('');
            select.value = current;
        }

        function loadEmployees() {
            api('/users').then(r => r.json()).then(d => {
                employeesCache = d.users || [];
                renderEmployees(employeesCache);
            }).catch(() => {});
        }

        function deleteNonAdmins() {
            if (!confirm('Удалить ВСЕХ пользователей, кроме администраторов? Профили и доступы будут удалены безвозвратно.')) return;
            if (!confirm('Точно удалить всех сотрудников? Действие необратимо.')) return;
            apiJson('/users/delete-non-admins', { method: 'POST' }).then(({ ok, data }) => {
                if (!ok) { alert(data.detail || 'Не удалось удалить (нужны права главного администратора).'); return; }
                alert(`Удалено пользователей: ${data.deleted}.`);
                loadEmployees();
            }).catch(err => alert('Ошибка: ' + err.message));
        }

        // ---- Combobox с поиском: клик по полю -> скроллируемый список + строка поиска ----
        function comboPositions() { return [...new Set(employeesCache.map(u => (u.position || '').trim()).filter(Boolean))].sort(); }
        function comboUserNames() { return [...new Set(employeesCache.map(u => (u.full_name || '').trim()).filter(Boolean))].sort(); }

        let comboTarget = null, comboAll = [];
        function comboEls() {
            let panel = document.getElementById('combo-panel');
            if (!panel) {
                panel = document.createElement('div');
                panel.className = 'combo-panel';
                panel.id = 'combo-panel';
                panel.innerHTML = `<input type="text" class="combo-search" id="combo-search" placeholder="Поиск…"><ul class="combo-list" id="combo-list"></ul>`;
                document.body.appendChild(panel);
                document.getElementById('combo-search').addEventListener('input', e => comboRender(e.target.value));
                document.getElementById('combo-search').addEventListener('keydown', e => { if (e.key === 'Escape') comboClose(); });
                document.addEventListener('mousedown', e => {
                    if (panel.classList.contains('open') && !panel.contains(e.target) && e.target !== comboTarget) comboClose();
                });
            }
            return panel;
        }
        function comboRender(query) {
            const q = (query || '').trim().toLowerCase();
            const items = comboAll.filter(v => v.toLowerCase().includes(q));
            const list = document.getElementById('combo-list');
            list.innerHTML = '';
            if (!items.length) { list.innerHTML = '<li class="empty">ничего не найдено</li>'; return; }
            items.forEach(v => {
                const li = document.createElement('li');
                li.textContent = v;
                li.addEventListener('mousedown', e => {
                    e.preventDefault();
                    if (comboTarget) { comboTarget.value = v; comboTarget.dispatchEvent(new Event('input')); }
                    comboClose();
                });
                list.appendChild(li);
            });
        }
        function comboOpen(input, items) {
            const panel = comboEls();
            comboTarget = input; comboAll = items;
            const r = input.getBoundingClientRect();
            panel.style.left = r.left + 'px';
            panel.style.top = (r.bottom + 4) + 'px';
            panel.style.width = Math.max(r.width, 240) + 'px';
            const s = document.getElementById('combo-search');
            s.value = '';
            comboRender('');
            panel.classList.add('open');
            s.focus();
        }
        function comboClose() { const p = document.getElementById('combo-panel'); if (p) p.classList.remove('open'); comboTarget = null; }

        // Привязка полей карточки к combobox (клик открывает список всех значений с поиском).
        (function () {
            const binds = { 'emp-position': comboPositions, 'emp-mentor': comboUserNames, 'emp-manager': comboUserNames };
            Object.entries(binds).forEach(([id, provider]) => {
                const el = document.getElementById(id);
                if (el) {
                    el.addEventListener('focus', () => comboOpen(el, provider()));
                    el.addEventListener('click', () => comboOpen(el, provider()));
                }
            });
        })();

        function roleBadge(user) {
            const cls = user.role === 'employee' ? 'planned' : 'active';
            return `<span class="status-badge ${cls}">${escapeHtml(ROLE_TITLES[user.role] || user.role)}</span>`;
        }

        function userActions(user) {
            const id = user.id;
            const isSelf = currentUser && currentUser.id === id;
            const buttons = [`<button class="icon-btn" onclick="showEmployeeSchedule('${id}')"><i data-lucide="calendar-days"></i> Расписание</button>`];

            // Обычный администратор правит только сотрудников — админов и главного трогать нельзя
            const manageable = isOwner || user.role === 'employee';
            if (manageable) {
                buttons.push(`<button class="icon-btn" onclick="editEmployee('${id}')"><i data-lucide="pencil"></i> Изменить</button>`);
                buttons.push(`<button class="icon-btn" onclick="openCredentialsDialog('${id}')"><i data-lucide="key-round"></i> Доступ</button>`);
                if (!isSelf) {
                    buttons.push(user.active
                        ? `<button class="icon-btn" onclick="setUserActive('${id}', false)"><i data-lucide="ban"></i> Заблокировать</button>`
                        : `<button class="icon-btn" onclick="setUserActive('${id}', true)"><i data-lucide="check"></i> Подтвердить</button>`);
                }
            }

            // Раздача прав и удаление — только у суперадмина. Суперадминов может быть
            // несколько: администратора можно повысить сразу до суперадмина, а другого
            // суперадмина — понизить (последнего сервер снять не даст).
            if (isOwner && !isSelf) {
                if (user.role === 'owner') {
                    buttons.push(`<button class="icon-btn" onclick="setUserRole('${id}', 'admin')"><i data-lucide="arrow-down"></i> Убрать из суперадминов</button>`);
                } else if (user.role === 'admin') {
                    buttons.push(`<button class="icon-btn" onclick="setUserRole('${id}', 'employee')"><i data-lucide="arrow-down"></i> Убрать из администраторов</button>`);
                    buttons.push(`<button class="icon-btn" onclick="setUserRole('${id}', 'owner')"><i data-lucide="crown"></i> Сделать суперадмином</button>`);
                } else {
                    buttons.push(`<button class="icon-btn" onclick="setUserRole('${id}', 'admin')"><i data-lucide="arrow-up"></i> Назначить администратором</button>`);
                }
                buttons.push(`<button class="icon-btn danger" onclick="deleteEmployee('${id}')"><i data-lucide="x"></i> Удалить</button>`);
            }
            return buttons.join(' ');
        }

        function renderEmployees(list) {
            const container = document.getElementById('employee-list');
            const pending = list.filter(u => !u.active);
            document.getElementById('pending-block').innerHTML = pending.length
                ? `<div class="warn"><i data-lucide="clock"></i> Ждут подтверждения: ${pending.map(u => escapeHtml(u.full_name)).join(', ')}.
                   Пока учётная запись не подтверждена, войти в систему нельзя.</div>`
                : '';

            if (!list.length) {
                container.innerHTML = `<div class="empty-hint">Пользователей пока нет — заведите первого в форме выше.</div>`;
                return;
            }
            container.innerHTML = `
                <table class="schedule">
                    <thead><tr>
                        <th>Пользователь</th><th>Роль</th><th>План адаптации</th>
                        <th>Дата выхода</th><th>Статус</th><th>Действия</th>
                    </tr></thead>
                    <tbody>${list.map(e => `
                        <tr${e.active ? '' : ' style="background:#fffbeb;"'}>
                            <td>
                                <strong>${escapeHtml(e.full_name)}</strong>
                                <div class="msg-meta">${e.username
                                    ? '@' + escapeHtml(e.username)
                                    : '<span style="color:#b45309;">нет логина — войти не может</span>'}
                                    ${e.must_change_credentials ? ' · сменит пароль при входе' : ''}</div>
                                <div class="msg-meta">${escapeHtml([e.position, e.department].filter(Boolean).join(' · ') || '—')}</div>
                                ${e.active ? '' : '<div class="msg-meta" style="color:#b45309;">не подтверждён</div>'}
                            </td>
                            <td>${roleBadge(e)}</td>
                            <td>
                                ${e.plan_title ? escapeHtml(e.plan_title) : '<span style="color:#dc2626;">не назначен</span>'}
                                ${e.plan_title && !e.plan_generated ? '<div class="msg-meta" style="color:#b45309;">ответы не сгенерированы</div>' : ''}
                            </td>
                            <td style="white-space:nowrap;">${escapeHtml(e.start_date || '—')}</td>
                            <td><span class="status-badge ${e.status}">${escapeHtml(EMPLOYEE_STATUS_TITLES[e.status] || e.status)}</span></td>
                            <td>${userActions(e)}</td>
                        </tr>
                    `).join('')}</tbody>
                </table>
            `;
        }

        function employeePayload(includeCredentials) {
            const payload = {};
            PROFILE_FIELDS.forEach(field => {
                payload[field] = document.getElementById(`emp-${field}`).value || null;
            });
            if (includeCredentials) {
                CREDENTIAL_FIELDS.forEach(field => {
                    payload[field] = document.getElementById(`emp-${field}`).value || null;
                });
            }
            return payload;
        }

        function resetEmployeeForm() {
            editingEmployeeId = null;
            PROFILE_FIELDS.concat(CREDENTIAL_FIELDS).forEach(field => {
                const el = document.getElementById(`emp-${field}`);
                el.value = field === 'status' ? 'planned' : '';
                el.disabled = false;
            });
            document.getElementById('employee-form-title').textContent = 'Новый сотрудник';
            document.getElementById('employee-status').textContent = '';
        }

        function editEmployee(id) {
            const employee = employeesCache.find(e => e.id === id);
            if (!employee) return;
            editingEmployeeId = id;
            PROFILE_FIELDS.forEach(field => {
                document.getElementById(`emp-${field}`).value = employee[field] || (field === 'status' ? 'planned' : '');
            });
            // Логин и пароль существующего пользователя меняются кнопкой «Доступ»
            CREDENTIAL_FIELDS.forEach(field => {
                const el = document.getElementById(`emp-${field}`);
                el.value = field === 'username' ? (employee.username || '') : '';
                el.disabled = true;
            });
            document.getElementById('employee-form-title').textContent = `Редактирование: ${employee.full_name}`;
            document.getElementById('employee-status').textContent = '';
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        function saveEmployee() {
            const isUpdate = Boolean(editingEmployeeId);
            const payload = employeePayload(!isUpdate);
            if (!payload.full_name) {
                document.getElementById('employee-status').textContent = 'Укажите ФИО';
                return;
            }
            const url = isUpdate ? `/users/${encodeURIComponent(editingEmployeeId)}` : '/users';
            apiJson(url, {
                method: isUpdate ? 'PUT' : 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            }).then(({ ok, data }) => {
                if (!ok) {
                    document.getElementById('employee-status').textContent = `${data.detail || 'Не удалось сохранить'}`;
                    return;
                }
                document.getElementById('employee-status').textContent = `Сохранён: ${data.full_name}`;
                resetEmployeeForm();
                loadEmployees();
            });
        }

        function deleteEmployee(id) {
            const employee = employeesCache.find(e => e.id === id);
            if (!confirm(`Удалить пользователя «${employee ? employee.full_name : id}»? Действие необратимо.`)) return;
            apiJson(`/users/${encodeURIComponent(id)}`, { method: 'DELETE' }).then(({ ok, data }) => {
                if (!ok) { alert(data.detail || 'Не удалось удалить'); return; }
                if (editingEmployeeId === id) resetEmployeeForm();
                document.getElementById('employee-schedule').innerHTML = '';
                loadEmployees();
            });
        }

        function setUserActive(id, active) {
            apiJson(`/users/${encodeURIComponent(id)}/active`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ active }),
            }).then(({ ok, data }) => {
                if (!ok) { alert(data.detail || 'Не удалось изменить'); return; }
                loadEmployees();
            });
        }

        function setUserRole(id, role) {
            const employee = employeesCache.find(e => e.id === id);
            const questions = {
                owner: `Сделать «${employee.full_name}» суперадмином? Он получит полный доступ: все документы всех администраторов и все сотрудники, раздача прав, удаление.`,
                admin: employee.role === 'owner'
                    ? `Убрать «${employee.full_name}» из суперадминов? Останется обычным администратором (только свой отдел и свои документы).`
                    : `Назначить «${employee.full_name}» администратором? Он получит доступ к базе знаний, конструктору планов и заведению сотрудников.`,
                employee: `Убрать «${employee.full_name}» из администраторов?`,
            };
            if (!confirm(questions[role] || 'Сменить роль?')) return;
            apiJson(`/users/${encodeURIComponent(id)}/role`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ role }),
            }).then(({ ok, data }) => {
                if (!ok) { alert(data.detail || 'Не удалось изменить роль'); return; }
                loadEmployees();
            });
        }

        function transferOwnership(id) {
            const employee = employeesCache.find(e => e.id === id);
            if (!confirm(`Передать права главного администратора пользователю «${employee.full_name}»?\n\n`
                       + `Вы станете обычным администратором и потеряете право удалять пользователей `
                       + `и раздавать права. Обе учётные записи будут разлогинены.`)) return;
            apiJson(`/users/${encodeURIComponent(id)}/transfer-ownership`, { method: 'POST' })
                .then(({ ok, data }) => {
                    if (!ok) { alert(data.detail || 'Не удалось передать права'); return; }
                    alert('Права переданы. Войдите заново.');
                    window.location.href = '/login';
                });
        }

        function openCredentialsDialog(id) {
            const employee = employeesCache.find(e => e.id === id);
            document.getElementById('cred-user-id').value = id;
            document.getElementById('cred-username').value = employee.username || '';
            document.getElementById('cred-password').value = '';
            document.getElementById('cred-title').textContent = `Доступ: ${employee.full_name}`;
            document.getElementById('cred-error').style.display = 'none';
            document.getElementById('credentials-dialog').showModal();
        }

        function submitCredentials() {
            const id = document.getElementById('cred-user-id').value;
            const errorBox = document.getElementById('cred-error');
            const username = document.getElementById('cred-username').value.trim();
            const password = document.getElementById('cred-password').value;
            if (!username && !password) {
                errorBox.textContent = 'Укажите логин или пароль';
                errorBox.style.display = 'block';
                return;
            }
            apiJson(`/users/${encodeURIComponent(id)}/credentials`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username: username || null, password: password || null }),
            }).then(({ ok, data }) => {
                if (!ok) {
                    errorBox.textContent = data.detail || 'Не удалось сохранить';
                    errorBox.style.display = 'block';
                    return;
                }
                document.getElementById('credentials-dialog').close();
                loadEmployees();
            });
        }

        function showEmployeeSchedule(id) {
            const container = document.getElementById('employee-schedule');
            container.innerHTML = `<div class="empty-hint">Считаем расписание...</div>`;
            apiJson(`/users/${encodeURIComponent(id)}/schedule`).then(({ ok, data }) => {
                if (!ok) {
                    container.innerHTML = `<div class="warn"><i data-lucide="triangle-alert"></i> ${escapeHtml(data.detail)}</div>`;
                    return;
                }
                const base = `/users/${encodeURIComponent(id)}/export`;
                const notGenerated = !data.plan_generated
                    ? `<div class="warn"><i data-lucide="triangle-alert"></i> У плана «${escapeHtml(data.plan_title)}» ещё не сгенерированы ответы.
                       Даты рассчитаны, тексты пустые — запустите генерацию на вкладке «Конструктор плана».</div>` : '';
                container.innerHTML = `
                    <h2 class="section-title"><i data-lucide="calendar-days"></i> Расписание: ${escapeHtml(data.employee.full_name)}</h2>
                    <div class="stage-hint">
                        План «${escapeHtml(data.plan_title)}» · дата выхода ${escapeHtml(data.start_date)} ·
                        сообщений: ${(data.messages || []).length}
                    </div>
                    ${notGenerated}
                    <div class="export-links">
                        <a href="${base}/schedule.md" download><i data-lucide="download"></i> schedule.md</a>
                        <a href="${base}/schedule.json" download><i data-lucide="download"></i> schedule.json</a>
                    </div>
                    <table class="schedule">
                        <thead><tr><th>Этап</th><th>Подэтап</th><th>Дата и время</th><th>Сообщение</th></tr></thead>
                        <tbody>${messageRows(data.messages, false)}</tbody>
                    </table>
                `;
                container.scrollIntoView({ behavior: 'smooth', block: 'start' });
            });
        }

        // ==================== Вопросы без ответа ====================
        function reasonLabel(r) {
            return r === 'escalate'
                ? '<span class="badge escalate"><i data-lucide="triangle-alert"></i> ЧС</span>'
                : '<span class="badge"><i data-lucide="search-x"></i> нет ответа</span>';
        }

        function updateQuestionsBadge(count) {
            const badge = document.getElementById('questions-badge');
            if (count > 0) { badge.textContent = count; badge.style.display = 'inline-block'; }
            else { badge.style.display = 'none'; }
        }

        function loadQuestions() {
            const showAll = document.getElementById('questions-show-all').checked;
            const url = showAll ? '/questions?status=' : '/questions?status=open';
            api(url).then(r => r.json()).then(d => {
                updateQuestionsBadge(d.open_count || 0);
                renderQuestions(d.questions || []);
            }).catch(() => {});
        }

        function refreshQuestionsBadge() {
            api('/questions?status=open').then(r => r.json())
                .then(d => updateQuestionsBadge(d.open_count || 0)).catch(() => {});
        }

        function renderQuestions(items) {
            const box = document.getElementById('questions-list');
            if (!items.length) {
                box.innerHTML = '<div class="empty-hint">Открытых вопросов нет <i data-lucide="party-popper"></i></div>';
                return;
            }
            box.innerHTML = items.map(q => {
                const who = [escapeHtml(q.user_name), escapeHtml(q.position)].filter(Boolean).join(', ');
                const contact = q.contact ? ` · ${escapeHtml(q.contact)}` : '';
                const resolved = q.resolved_question && q.resolved_question !== q.question
                    ? `<div class="context-note"><i data-lucide="link-2"></i> Понято как: «${escapeHtml(q.resolved_question)}»</div>` : '';
                if (q.status === 'resolved') {
                    return `<div class="card" style="opacity:.75;">
                        <div>${reasonLabel(q.reason)} <small style="color:#94a3b8;">${escapeHtml(q.created_at)}</small></div>
                        <div style="margin:6px 0;"><strong><i data-lucide="help-circle"></i> ${escapeHtml(q.question)}</strong> — ${who}${contact}</div>
                        <div class="answer"><i data-lucide="check"></i> ${escapeHtml(q.answer)}</div>
                        <small style="color:#94a3b8;">Ответил: ${escapeHtml(q.answered_by || '')} · ${escapeHtml(q.answered_at || '')}</small>
                    </div>`;
                }
                return `<div class="card">
                    <div>${reasonLabel(q.reason)} <small style="color:#94a3b8;">${escapeHtml(q.created_at)}</small></div>
                    <div style="margin:6px 0;"><strong><i data-lucide="help-circle"></i> ${escapeHtml(q.question)}</strong></div>
                    <div style="font-size:13px;color:#475569;">От: ${who}${contact}${q.mentor ? ' · наставник: ' + escapeHtml(q.mentor) : ''}</div>
                    ${resolved}
                    <textarea id="ans-${q.id}" rows="3" style="width:100%;margin-top:8px;" placeholder="Ответ сотруднику (можно после консультации со специалистом)"></textarea>
                    <button class="primary-btn" style="margin-top:6px;" onclick="resolveQuestion('${q.id}')"><i data-lucide="send-horizontal"></i> Отправить ответ</button>
                </div>`;
            }).join('');
            refreshIcons();
        }

        function resolveQuestion(qid) {
            const answer = (document.getElementById('ans-' + qid).value || '').trim();
            if (!answer) { alert('Введите ответ'); return; }
            apiJson(`/questions/${encodeURIComponent(qid)}/resolve`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ answer }),
            }).then(({ ok, data }) => {
                if (!ok) { alert(data.detail || 'Не удалось сохранить ответ'); return; }
                loadQuestions();
            });
        }

        refreshQuestionsBadge();
