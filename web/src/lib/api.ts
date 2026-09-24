// FastAPI（docs/api-design.md v0.3）の型付きクライアント
export const API = import.meta.env.PUBLIC_API_URL ?? 'http://localhost:8000';

export type RoadClass = 'auto' | 'all' | 'trunk' | 'major';
export type Tier = 'ok' | 'heavy';

export interface Health {
	status: 'ok' | 'loading' | 'error';
	graph_loaded: boolean;
	links?: number;
	nodes?: number;
	load_seconds?: number;
	error?: string;
}

export interface Reachability {
	origin: { lat: number; lon: number };
	snap: { lat: number; lon: number; dist_m: number };
	limit_min: number;
	use_expressway: boolean;
	road_class_applied: RoadClass;
	tier: Tier;
	counts: Record<'all' | 'trunk' | 'major', number>;
	count: number;
	elapsed_ms: number;
	link_ids: number[];
	costs: number[];
}

export interface Route {
	geometry: { type: 'LineString'; coordinates: [number, number][] } | null;
	summary: { dist_m: number; time_min: number; link_count: number };
	snap: { origin_m: number; dest_m: number };
	unreachable_reason: string | null;
	elapsed_ms: number;
}

export interface Bookmark {
	type: 'Feature';
	id: number;
	geometry: { type: 'Point'; coordinates: [number, number] };
	properties: { name: string; memo: string | null; limit_min: number; use_expressway: boolean; created_at: string };
}

export class ApiError extends Error {
	constructor(public status: number, detail: string) {
		super(detail);
	}
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
	const res = await fetch(API + path, init);
	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		const d = body.detail;
		throw new ApiError(res.status, typeof d === 'string' ? d : JSON.stringify(d ?? res.statusText));
	}
	return res.status === 204 ? (undefined as T) : res.json();
}

const q = (o: Record<string, string | number | boolean>) =>
	'?' + Object.entries(o).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&');

export const api = {
	health: () => call<Health>('/health'),
	reachability: (p: { lat: number; lon: number; limit_min: number; use_expressway: boolean; road_class?: RoadClass }) =>
		call<Reachability>('/reachability' + q({ road_class: 'auto', ...p })),
	route: (p: { from_lat: number; from_lon: number; to_lat: number; to_lon: number; use_expressway: boolean }) =>
		call<Route>('/route' + q(p)),
	bookmarks: {
		list: () => call<{ type: 'FeatureCollection'; features: Bookmark[] }>('/bookmarks'),
		create: (b: { name: string; memo?: string; lat: number; lon: number; limit_min: number; use_expressway: boolean }) =>
			call<Bookmark>('/bookmarks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) }),
		remove: (id: number) => call<void>(`/bookmarks/${id}`, { method: 'DELETE' }),
		reachability: (id: number) => call<Reachability>(`/bookmarks/${id}/reachability`)
	}
};
