<script lang="ts">
	import { onMount } from 'svelte';
	import maplibregl, { type Map, type MapMouseEvent, type ExpressionSpecification } from 'maplibre-gl';
	import { Protocol } from 'pmtiles';
	import { api, ApiError, type Health, type Reachability, type Route, type Bookmark } from '$lib/api';

	// 全国 PMTiles（feature id = link_id）。到達圏は setFeatureState で着色する（設計書 1-1）
	const SOURCE = 'roads';
	const SOURCE_LAYER = 'roads';
	const TILES = `pmtiles://${location.origin}/tiles/roads_nationwide.pmtiles`; // 本番は Worker が R2 から、ローカルは static/tiles/
	const LIMITS = [15, 30, 60, 120, 240, 480, 720, 1200, 1920]; // 東京起点は 1,200 分、鹿児島起点は 1,800 分で本州・四国・九州の全域に到達。1,920 = 80 分 × 24 帯

	type LatLon = { lat: number; lon: number };

	let mapEl: HTMLDivElement;
	let map: Map | undefined;

	// ---- 状態（Runes）
	let health = $state<Health | null>(null);
	let mode = $state<'reach' | 'route'>('reach');
	let limitMin = $state(120);
	let useExpressway = $state(true);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let origin = $state<LatLon | null>(null);
	let dest = $state<LatLon | null>(null);
	let reach = $state<Reachability | null>(null);
	let route = $state<Route | null>(null);
	let paintMs = $state(0);
	let bookmarks = $state<Bookmark[]>([]);
	let bmName = $state('');
	let bmError = $state<string | null>(null);
	let overlayReady = $state(false);

	const ready = $derived((health?.graph_loaded ?? false) && overlayReady);
	const hasBookmarks = $derived(health?.features?.includes('bookmarks') ?? false); // DB なし（公開版）では出さない

	// ---- 地図
	// 到達時間の色: 離散バンド（近い=暖色 → 遠い=寒色）。連続グラデーションは中間の淡い黄色が淡色地図に溶けて「空洞」に見えたので離散に変えた。
	// 480 分までは 6 等分（15 分なら 2.5 分刻み、480 分なら 80 分刻み）。480 分超は 80 分刻みで固定し、帯の数を増やす（720 → 9 本、1,200 → 15 本）。
	// 色は旧ビューワー（shiwaku/ksj-route-search-api viewer/index.html の RANK_COLORS）の 10 色をアンカーに、RGB で線形補間して帯の数だけ作る（10 本のときはアンカーそのまま）
	const BAND_COLORS = ['#ff0000', '#ff4000', '#ff8000', '#ffc000', '#ffff00', '#c0ff00', '#00cc00', '#00cc80', '#00cccc', '#440055'];
	const bandWidth = (limit: number) => (limit <= 480 ? limit / 6 : 80);
	const bandCount = (limit: number) => Math.ceil(limit / bandWidth(limit));
	const bandEdges = (limit: number) => Array.from({ length: bandCount(limit) }, (_, i) => Math.round(Math.min(limit, bandWidth(limit) * (i + 1))));
	function bandColors(limit: number): string[] {
		const n = bandCount(limit);
		if (n === BAND_COLORS.length) return BAND_COLORS;
		const rgb = BAND_COLORS.map((h) => [1, 3, 5].map((k) => parseInt(h.slice(k, k + 2), 16)));
		return Array.from({ length: n }, (_, i) => {
			const t = (i / (n - 1)) * (rgb.length - 1);
			const a = Math.floor(t), b = Math.min(a + 1, rgb.length - 1), f = t - a;
			const c = rgb[a].map((v, k) => Math.round(v + (rgb[b][k] - v) * f));
			return '#' + c.map((v) => v.toString(16).padStart(2, '0')).join('');
		});
	}
	function colorRamp(limit: number): ExpressionSpecification {
		const edges = bandEdges(limit), colors = bandColors(limit);
		const steps: (string | number)[] = [colors[0]];
		for (let i = 1; i < colors.length; i++) steps.push(edges[i - 1], colors[i]);
		return ['step', ['coalesce', ['feature-state', 'cost'], 0], ...steps] as unknown as ExpressionSpecification;
	}
	// 凡例の目盛り: 帯が 8 本を超えたら 3 本ごとに表示（15 本なら 240・480・…・1,200）
	const edgeLabel = (limit: number, i: number) => (bandCount(limit) <= 8 || (i + 1) % 3 === 0 ? String(bandEdges(limit)[i]) : '');
	// 状態のあるリンクだけ描く。さらにズームが浅いほど太い道だけ（road_type 1=高速 3=国道 5=県道 7=市区町村道）。
	// タイルは z13 まで間引かれるので、細い道は広域では「点々」にしか見えない → 出さない
	// タイルの中身は z5-8 が高速・国道、z9-10 が＋都道府県道、z11 以上が全部（make_pmtiles.py・issue #17）。
	// ここでは z<11 を 1・3・5 に絞る（z5-8 はタイルに 5 が入っていないので、結果は高速・国道だけになる）
	const reachOpacity: ExpressionSpecification = ['step', ['zoom'],
		['case', ['all', ['!=', ['feature-state', 'cost'], null], ['in', ['get', 'road_type'], ['literal', [1, 3, 5]]]], 0.95, 0],
		11, ['case', ['!=', ['feature-state', 'cost'], null], 0.95, 0]];
	// ズーム式は最外側にしか置けない（['+', ['interpolate', ['zoom'] ...], 2.5] はスタイルエラー）ので縁取りは別に書く
	const reachWidth: ExpressionSpecification = ['interpolate', ['linear'], ['zoom'], 6, 1.2, 9, 1.6, 13, 2.5];   // 旧ビューワーは固定 1.5 px。9/10 までの 2〜4.5 は太過ぎた
	const casingWidth: ExpressionSpecification = ['interpolate', ['linear'], ['zoom'], 6, 2.4, 9, 3, 13, 4.2];

	// 到達圏・経路・地点のレイヤ。道路は注記（symbol）の下、経路と地点は最前面
	function addOverlay() {
		const m = map!;
		m.addSource(SOURCE, { type: 'vector', url: TILES });
		m.addSource('route', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
		m.addSource('points', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
		// 背景スタイルは symbol（注記）が途中にも挟まるので「最初の symbol の前」では道路・建物に被られる。
		// 末尾に続く注記ブロックの先頭を探し、その直前＝地物の一番上に置く
		const layers = m.getStyle().layers;
		let i = layers.length;
		while (i > 0 && layers[i - 1].type === 'symbol') i--;
		const firstSymbol = layers[i]?.id;
		// パン対策: 3 レイヤとも画面内の全リンクを毎フレーム描くので、レイヤ数がそのまま重さになる。
		// 薄い全道路は z11 以上（背景の地理院タイルに道路があるので広域では要らない）、白縁取りは z10 以上だけ描く
		m.addLayer({ id: 'roads-base', type: 'line', source: SOURCE, 'source-layer': SOURCE_LAYER, minzoom: 11,
			paint: { 'line-color': '#bbbbbb', 'line-width': ['interpolate', ['linear'], ['zoom'], 6, 0.3, 13, 1.2],
				'line-opacity': ['interpolate', ['linear'], ['zoom'], 6, 0.15, 13, 0.4] } }, firstSymbol);
		// 繋ぎ目対策: 既定の line-cap は butt（角切り）で、リンク境界ごとに切れ目が見える（平均リンク長 100 m・広域では 1 px 未満）。
		// square は線幅の半分だけ両端を延ばすので隣と重なって繋がる。round は端ごとに扇形を描くため
		// z8 のパンが 103 → 233 ms と 2 倍重くなった（square は 109 ms）。join は頂点が 3 つ以上のリンク（z10 以上）にしか効かない
		const roundJoin = { 'line-cap': 'square', 'line-join': 'round' } as const;
		m.addLayer({ id: 'roads-reach-casing', type: 'line', source: SOURCE, 'source-layer': SOURCE_LAYER, layout: roundJoin, minzoom: 10,
			paint: { 'line-color': '#ffffff', 'line-width': casingWidth, 'line-opacity': reachOpacity } }, firstSymbol);
		m.addLayer({ id: 'roads-reach', type: 'line', source: SOURCE, 'source-layer': SOURCE_LAYER, layout: roundJoin,
			paint: { 'line-color': colorRamp(limitMin), 'line-width': reachWidth, 'line-opacity': reachOpacity } }, firstSymbol);
		m.addLayer({ id: 'route', type: 'line', source: 'route', layout: { 'line-cap': 'round', 'line-join': 'round' },
			paint: { 'line-color': '#0055ff', 'line-width': 4, 'line-opacity': 0.9 } });
		m.addLayer({ id: 'points', type: 'circle', source: 'points',
			paint: { 'circle-radius': 8, 'circle-color': ['match', ['get', 'kind'], 'origin', '#00aa44', '#dd2222'],
				'circle-stroke-color': '#fff', 'circle-stroke-width': 2 } });
		overlayReady = true;
	}

	onMount(() => {
		maplibregl.addProtocol('pmtiles', new Protocol().tile);
		// 背景: 国土地理院 最適化ベクトルタイル「淡色地図風」（gsi-cyberjapan/3dpc-3dtiles の pale.json を static/styles に同梱）。
		// タイル・glyph・sprite は地理院のサーバーから。到達圏などのレイヤは style.load 後に重ねる
		map = new maplibregl.Map({
			container: mapEl,
			center: [139.767, 35.681],
			zoom: 9,
			hash: true, // URL に #zoom/lat/lon を持たせる（リロード・共有で同じ場所に戻れる）
			style: '/styles/pale.json'
		});
		map.on('load', addOverlay);
		map.on('sourcedata', onTileData);
		if (import.meta.env.DEV) (window as unknown as { __map: Map }).__map = map; // 開発時のみ: Playwright から計測するため
		map.addControl(new maplibregl.NavigationControl(), 'top-right');
		map.addControl(new maplibregl.ScaleControl());
		map.on('click', onClick);

		// 応答を待ってから 1 秒後に次を投げる。公開版はコンテナが寝ていると最初の 1 本が起動まで返らない
		// （コールドスタート）ので、setInterval だと待ちのリクエストが積み上がる
		let stopped = false, timer: ReturnType<typeof setTimeout>;
		const poll = async () => {
			try { health = await api.health(); } catch { health = { status: 'loading', graph_loaded: false }; }
			if (stopped) return;
			if (health.graph_loaded) { if (health.features?.includes('bookmarks')) loadBookmarks(); }
			else timer = setTimeout(poll, 1000);
		};
		poll();
		return () => { stopped = true; clearTimeout(timer); map?.remove(); };
	});

	// 打ち切り時間が変わったら色の目盛りも合わせる。
	// ⚠️ 依存する $state は早期 return の前に読む。以前は map.getLayer() が undefined の初回に limitMin を
	// 読まずに抜けたため依存が登録されず、480 分でも目盛りが 120 分のまま（水戸 90 分が藍色）になっていた
	$effect(() => {
		const ramp = colorRamp(limitMin);
		if (!overlayReady) return;
		map!.setPaintProperty('roads-reach', 'line-color', ramp);
	});


	const idle = () => new Promise<void>((r) => map!.once('idle', () => r()));

	function setPoints() {
		const f = [];
		if (origin) f.push({ type: 'Feature', properties: { kind: 'origin' }, geometry: { type: 'Point', coordinates: [origin.lon, origin.lat] } });
		if (dest) f.push({ type: 'Feature', properties: { kind: 'dest' }, geometry: { type: 'Point', coordinates: [dest.lon, dest.lat] } });
		(map!.getSource('points') as maplibregl.GeoJSONSource).setData({ type: 'FeatureCollection', features: f as never });
	}

	function clearReach() {
		map?.removeFeatureState({ source: SOURCE, sourceLayer: SOURCE_LAYER });
		reach = null;
		reachCost = null;
		painted.clear();
	}
	function clearRoute() {
		(map?.getSource('route') as maplibregl.GeoJSONSource | undefined)?.setData({ type: 'FeatureCollection', features: [] });
		route = null;
	}

	// ---- 到達圏: API → setFeatureState で着色。描画時間も測って表示する
	// 塗るのは「読み込み済みのタイルに入っているリンク」だけ（issue #8）。
	// MapLibre は setFeatureState のたびに、読み込み済みタイル 1 枚ごとに変更した ID を全部照合する（タイル枚数 × 本数）。
	// 120 分の 78.6 万本を一度に塗ると 1 フレームで 1.8〜2 秒止まっていた。見えている分（z13 で約 2 万本）だけなら 0.2 秒。
	// 地図を動かして新しいタイルが来たら、その分を塗り足す（onTileData）
	// Map は maplibre-gl の型名と衝突する（import type { Map }）ので globalThis.Map と書く
	let reachCost: globalThis.Map<number, number> | null = null; // link_id → コスト（到達したリンク全部）
	const painted = new Set<number>(); // 塗り済みの link_id

	function paintLoaded() {
		if (!reachCost || !map) return;
		for (const f of map.querySourceFeatures(SOURCE, { sourceLayer: SOURCE_LAYER })) {
			const id = f.id as number;
			if (painted.has(id)) continue;
			painted.add(id);
			const c = reachCost.get(id);
			if (c !== undefined) map.setFeatureState({ source: SOURCE, sourceLayer: SOURCE_LAYER, id }, { cost: c });
		}
	}

	// タイルが届くたびに呼ばれる。まとめて届くので 1 フレームに 1 回だけ塗り足す
	let tileFrame = 0;
	function onTileData(e: maplibregl.MapSourceDataEvent) {
		if (e.sourceId !== SOURCE || !e.tile || !reachCost || tileFrame) return;
		tileFrame = requestAnimationFrame(() => { tileFrame = 0; paintLoaded(); });
	}

	async function paint(r: Reachability) {
		const t0 = performance.now();
		map!.removeFeatureState({ source: SOURCE, sourceLayer: SOURCE_LAYER });
		painted.clear();
		const { link_ids, costs } = r;
		reachCost = new globalThis.Map();
		for (let i = 0; i < link_ids.length; i++) reachCost.set(link_ids[i], costs[i]);
		paintLoaded();
		await idle();
		paintMs = Math.round(performance.now() - t0);
	}

	async function runReach(p: LatLon) {
		busy = true; error = null;
		try {
			const r = await api.reachability({ lat: p.lat, lon: p.lon, limit_min: limitMin, use_expressway: useExpressway });
			reach = r;
			await paint(r);
		} catch (e) {
			error = e instanceof ApiError ? `${e.status}: ${e.message}` : String(e);
		} finally { busy = false; }
	}

	// ---- 経路
	async function runRoute(a: LatLon, b: LatLon) {
		busy = true; error = null;
		try {
			const r = await api.route({ from_lat: a.lat, from_lon: a.lon, to_lat: b.lat, to_lon: b.lon, use_expressway: useExpressway });
			route = r;
			(map!.getSource('route') as maplibregl.GeoJSONSource).setData({
				type: 'FeatureCollection',
				features: r.geometry ? [{ type: 'Feature', properties: {}, geometry: r.geometry }] : []
			});
		} catch (e) {
			error = e instanceof ApiError ? `${e.status}: ${e.message}` : String(e);
		} finally { busy = false; }
	}

	function onClick(e: MapMouseEvent) {
		if (!ready || busy) return;
		const p = { lat: e.lngLat.lat, lon: e.lngLat.lng };
		if (mode === 'reach') {
			origin = p; setPoints(); runReach(p);
		} else if (!origin || dest) {
			origin = p; dest = null; clearRoute(); setPoints();
		} else {
			dest = p; setPoints(); runRoute(origin, dest);
		}
	}

	// 条件を変えたら同じ地点で再実行
	function rerun() {
		if (!origin) return;
		if (mode === 'reach') runReach(origin);
		else if (dest) runRoute(origin, dest);
	}

	function switchMode(m: 'reach' | 'route') {
		mode = m; origin = null; dest = null; error = null;
		clearReach(); clearRoute(); setPoints();
	}

	// ---- ブックマーク（PostgreSQL / PostGIS・設計書 4 章）
	async function loadBookmarks() {
		try { bookmarks = (await api.bookmarks.list()).features; bmError = null; }
		catch (e) { bmError = e instanceof ApiError ? e.message : 'データベースに接続できません'; }
	}
	async function saveBookmark() {
		if (!origin || !bmName.trim()) return;
		try {
			await api.bookmarks.create({ name: bmName.trim(), lat: origin.lat, lon: origin.lon, limit_min: limitMin, use_expressway: useExpressway });
			bmName = ''; await loadBookmarks();
		} catch (e) { bmError = e instanceof ApiError ? e.message : String(e); }
	}
	async function showBookmark(b: Bookmark) {
		if (mode !== 'reach') switchMode('reach');
		busy = true; error = null;
		try {
			limitMin = b.properties.limit_min; useExpressway = b.properties.use_expressway;
			const r = await api.bookmarks.reachability(b.id);
			origin = r.origin; setPoints(); reach = r;
			map!.flyTo({ center: [r.origin.lon, r.origin.lat], zoom: 9 });
			await paint(r);
		} catch (e) { error = e instanceof ApiError ? `${e.status}: ${e.message}` : String(e); }
		finally { busy = false; }
	}
	async function removeBookmark(id: number) {
		try { await api.bookmarks.remove(id); await loadBookmarks(); }
		catch (e) { bmError = e instanceof ApiError ? e.message : String(e); }
	}

	const fmt = (n: number) => n.toLocaleString();
</script>

<div class="relative h-screen w-screen">
	<div bind:this={mapEl} class="h-full w-full"></div>

	<aside class="absolute top-3 left-3 flex max-h-[calc(100vh-1.5rem)] w-80 flex-col gap-3 overflow-y-auto rounded-lg bg-white/95 p-4 text-sm shadow-lg">
		<header>
			<h1 class="text-base font-bold">到達圏・経路探索</h1>
			<p class="mt-1 text-xs text-gray-600">
				{#if ready}
					<span class="text-green-700">● API 準備完了</span> — {fmt(health!.links!)} リンク / 起動 {health!.load_seconds}s
				{:else if health?.status === 'error'}
					<span class="text-red-700">● API エラー: {health.error}</span>
				{:else}
					<span class="animate-pulse text-amber-600">● API 起動中…（しばらく使われていないと 1 分ほど掛かります）</span>
				{/if}
			</p>
		</header>

		<div class="flex rounded border border-gray-300 text-center">
			<button class="flex-1 py-1 {mode === 'reach' ? 'bg-[#0060df] text-white' : ''}" onclick={() => switchMode('reach')}>到達圏</button>
			<button class="flex-1 py-1 {mode === 'route' ? 'bg-[#0060df] text-white' : ''}" onclick={() => switchMode('route')}>経路探索</button>
		</div>

		<p class="text-xs text-gray-600">
			{#if mode === 'reach'}地図をクリック → その地点から <b>{limitMin} 分</b>で行ける道路を着色
			{:else}地図を 2 回クリック（<span style="color:#00aa44">●</span>起点 → <span style="color:#dd2222">●</span>終点）{/if}
		</p>

		{#if mode === 'reach'}
			<label class="flex items-center justify-between gap-2">
				<span>打ち切り</span>
				<select class="rounded border px-2 py-1" bind:value={limitMin} onchange={rerun} disabled={busy}>
					{#each LIMITS as l}<option value={l}>{l} 分</option>{/each}
				</select>
			</label>
		{/if}
		<label class="flex items-center justify-between gap-2">
			<span>高速道路を使う</span>
			<input type="checkbox" class="h-4 w-4" bind:checked={useExpressway} onchange={rerun} disabled={busy} />
		</label>

		{#if mode === 'reach'}
			<div class="text-xs">
				<div class="flex">
					{#each bandColors(limitMin) as c}<div class="h-2.5 flex-1 first:rounded-l last:rounded-r" style="background:{c}"></div>{/each}
				</div>
				<div class="flex text-gray-600">
					{#each bandEdges(limitMin) as _, i}<span class="flex-1 text-right">{edgeLabel(limitMin, i)}</span>{/each}
				</div>
				<p class="mt-0.5 text-gray-500">分（到達に掛かる時間）。広域では太い道だけを表示</p>
				<p class="text-gray-500">速度の前提: 高速 80 / 国道 35 / 都道府県道 30 / 市区町村道 20 km/h</p>
			</div>
		{/if}

		{#if busy}<p class="animate-pulse text-amber-700">計算中…</p>{/if}
		{#if error}<p class="rounded bg-red-50 p-2 text-red-700">{error}</p>{/if}

		{#if mode === 'reach' && reach}
			<dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 rounded bg-gray-50 p-2 text-xs">
				<dt class="text-gray-500">到達リンク</dt><dd class="font-mono">{fmt(reach.count)} 本</dd>
				<dt class="text-gray-500">道路種別</dt>
				<dd>{reach.road_class_applied === 'all' ? '全部' : reach.road_class_applied === 'trunk' ? '高速＋国道＋県道' : '高速＋国道'}
					{#if reach.road_class_applied !== 'all'}<span class="text-amber-700">（広域のため幹線のみ）</span>{/if}</dd>
				<dt class="text-gray-500">スナップ</dt><dd class="font-mono">{Math.round(reach.snap.dist_m)} m</dd>
				<dt class="text-gray-500">API / 描画</dt><dd class="font-mono">{Math.round(reach.elapsed_ms)} ms / {paintMs} ms</dd>
				{#if reach.tier === 'heavy'}<dd class="col-span-2 text-amber-700">⚠ 40 万本超。低スペック端末では重い</dd>{/if}
			</dl>
		{/if}

		{#if mode === 'route' && route}
			<dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 rounded bg-gray-50 p-2 text-xs">
				{#if route.geometry}
					<dt class="text-gray-500">距離</dt><dd class="font-mono">{(route.summary.dist_m / 1000).toFixed(1)} km</dd>
					<dt class="text-gray-500">所要</dt><dd class="font-mono">{Math.floor(route.summary.time_min / 60)} 時間 {Math.round(route.summary.time_min % 60)} 分</dd>
					<dt class="text-gray-500">リンク</dt><dd class="font-mono">{fmt(route.summary.link_count)} 本</dd>
					<dt class="text-gray-500">API</dt><dd class="font-mono">{Math.round(route.elapsed_ms)} ms</dd>
				{:else}
					<dd class="col-span-2 text-red-700">到達不能: {route.unreachable_reason}</dd>
				{/if}
			</dl>
		{/if}

		{#if hasBookmarks}
		<section class="border-t pt-2">
			<h2 class="mb-1 font-semibold">ブックマーク</h2>
			{#if bmError}<p class="mb-1 text-xs text-red-700">{bmError}</p>{/if}
			<form class="mb-2 flex gap-1" onsubmit={(e) => { e.preventDefault(); saveBookmark(); }}>
				<input class="flex-1 rounded border px-2 py-1" placeholder={origin ? '地点名を入力' : '地図をクリックしてから'} bind:value={bmName} disabled={!origin} />
				<button class="rounded bg-[#0060df] px-2 py-1 text-white disabled:opacity-40" disabled={!origin || !bmName.trim()}>保存</button>
			</form>
			<ul class="max-h-40 space-y-1 overflow-y-auto text-xs">
				{#each bookmarks as b (b.id)}
					<li class="flex items-center gap-1 rounded bg-gray-50 px-2 py-1">
						<button class="flex-1 text-left hover:underline" onclick={() => showBookmark(b)} title="この地点から到達圏を再実行">
							{b.properties.name} <span class="text-gray-500">{b.properties.limit_min} 分{b.properties.use_expressway ? '' : '・高速なし'}</span>
						</button>
						<button class="text-gray-400 hover:text-red-600" onclick={() => removeBookmark(b.id)} title="削除">✕</button>
					</li>
				{:else}
					<li class="text-gray-500">まだありません</li>
				{/each}
			</ul>
		</section>
		{/if}
	</aside>
</div>
