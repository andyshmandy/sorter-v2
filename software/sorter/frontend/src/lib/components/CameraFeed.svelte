<script lang="ts">
	import { getMachineContext } from '$lib/machines/context';
	import type { DashboardFeedCrop } from '$lib/dashboard/crops';
	import LiveImage from '$lib/components/LiveImage.svelte';
	import StreamControlsOverlay from '$lib/components/StreamControlsOverlay.svelte';
	import { WifiOff, VideoOff } from 'lucide-svelte';
	import Spinner from '$lib/components/Spinner.svelte';
	import type { Snippet } from 'svelte';
	import { roleView } from '$lib/video';

	type ControlKey = 'annotations' | 'crop' | 'zones' | 'fullscreen';

	let {
		camera,
		label = '',
		showHeader = true,
		framed = true,
		crop = null,
		defaultAnnotated = true,
		defaultCropped = undefined,
		defaultZones = true,
		controls = ['annotations'],
		layer = $bindable('annotated'),
		headerActions = null
	}: {
		camera: string;
		label?: string;
		showHeader?: boolean;
		framed?: boolean;
		crop?: DashboardFeedCrop | null;
		defaultAnnotated?: boolean;
		defaultCropped?: boolean;
		defaultZones?: boolean;
		controls?: ControlKey[];
		layer?: 'raw' | 'annotated';
		headerActions?: Snippet | null;
	} = $props();

	const ctx = getMachineContext();

	// Persistent per-camera toggle state — survives reloads via localStorage.
	// Keyed by camera so e.g. c_channel_2's crop toggle doesn't leak into
	// the carousel's. Falls back to the ``default*`` props when no saved
	// value exists.
	const storageKey = (key: string) => `camera-feed:${camera}:${key}`;

	function readPersisted(key: string, fallback: boolean): boolean {
		if (typeof localStorage === 'undefined') return fallback;
		try {
			const raw = localStorage.getItem(storageKey(key));
			if (raw === null) return fallback;
			return raw === '1' || raw === 'true';
		} catch {
			return fallback;
		}
	}

	function writePersisted(key: string, value: boolean) {
		if (typeof localStorage === 'undefined') return;
		try {
			localStorage.setItem(storageKey(key), value ? '1' : '0');
		} catch {
			// Quota / private mode — silently ignore.
		}
	}

	/* svelte-ignore state_referenced_locally */
	let annotated = $state(readPersisted('annotated', defaultAnnotated && layer === 'annotated'));
	// Legacy: presence of `crop` prop defaulted cropping on. Honor that unless
	// the caller explicitly sets `defaultCropped`.
	/* svelte-ignore state_referenced_locally */
	let cropped = $state(readPersisted('cropped', defaultCropped ?? crop !== null));
	/* svelte-ignore state_referenced_locally */
	let zones = $state(readPersisted('zones', defaultZones));

	// Keep legacy `layer` prop synced with new `annotated` state so existing
	// consumers (e.g. dashboard) binding to `layer` keep working.
	$effect(() => {
		layer = annotated ? 'annotated' : 'raw';
	});
	$effect(() => {
		annotated = layer === 'annotated';
	});

	// Write-back side: every toggle change writes to localStorage.
	$effect(() => {
		writePersisted('annotated', annotated);
	});
	$effect(() => {
		writePersisted('cropped', cropped);
	});
	$effect(() => {
		writePersisted('zones', zones);
	});

	const showAnnotations = $derived(controls.includes('annotations'));
	const showCrop = $derived(controls.includes('crop'));
	const showZones = $derived(controls.includes('zones'));
	const showFullscreen = $derived(controls.includes('fullscreen'));

	let fullscreenOpen = $state(false);
	let stale = $state(false);

	function handleFullscreenKey(event: KeyboardEvent) {
		if (event.key === 'Escape' && fullscreenOpen) {
			fullscreenOpen = false;
		}
	}

	const configuredSource = $derived(ctx.machine?.camerasConfig?.cameras?.[camera]);
	const hasCameraConfig = $derived(Boolean(ctx.machine?.camerasConfig?.cameras));
	const reportedHealth = $derived(
		ctx.cameraHealth.get(camera) ??
			(hasCameraConfig && configuredSource == null ? 'unassigned' : 'unknown')
	);
	// A camera that reports in but whose frames stopped coming looks like one
	// reconnecting.
	const health = $derived(
		stale && (reportedHealth === 'online' || reportedHealth === 'unknown')
			? 'reconnecting'
			: reportedHealth
	);
	const is_healthy = $derived(health === 'online' || health === 'unknown');
	const is_configured = $derived(health !== 'unassigned');

	const display_label = $derived(label || camera);
</script>

<div
	class={`flex h-full min-h-0 flex-col overflow-hidden ${
		fullscreenOpen
			? 'fixed inset-0 z-50 !h-screen !w-screen bg-black p-4'
			: framed
				? 'setup-card-shell border'
				: 'setup-card-body'
	}`}
>
	{#if showHeader}
		<div
			class="setup-card-header flex flex-shrink-0 items-center justify-between px-3 py-2 text-sm"
		>
			<span class="font-medium text-text">{display_label}</span>
			{#if headerActions}
				<div class="flex shrink-0 items-center gap-1">
					{@render headerActions()}
				</div>
			{/if}
		</div>
	{/if}
	<div class="setup-card-body relative flex-1 overflow-hidden">
		{#if is_configured}
			<LiveImage
				view={roleView(camera, annotated, cropped)}
				alt={display_label}
				class="absolute inset-0 h-full w-full object-contain {is_healthy ? '' : 'opacity-30'}"
				bind:stale
			/>
		{/if}

		{#if !is_healthy}
			<div class="absolute inset-0 flex items-center justify-center">
				<div class="flex flex-col items-center gap-2 text-center">
					{#if health === 'reconnecting'}
						<Spinner size={28} class="text-text-muted" />
						<span class="text-sm font-medium text-text-muted">Reconnecting...</span>
					{:else if health === 'offline'}
						<WifiOff size={28} class="text-text-muted" />
						<span class="text-sm font-medium text-text-muted">Camera Offline</span>
					{:else if health === 'unassigned'}
						<VideoOff size={28} class="text-text-muted" />
						<span class="text-sm font-medium text-text-muted">No Camera Assigned</span>
					{/if}
				</div>
			</div>
		{/if}

		<StreamControlsOverlay
			bind:annotated
			bind:cropped
			bind:zones
			bind:fullscreen={fullscreenOpen}
			{showAnnotations}
			{showCrop}
			{showZones}
			{showFullscreen}
		/>

		{#if fullscreenOpen}
			<div
				class="pointer-events-none absolute top-3 left-3 z-20 border border-white/20 bg-black/55 px-2 py-0.5 text-xs text-white/80 shadow-md backdrop-blur-sm"
			>
				Esc or toggle to exit
			</div>
		{/if}
	</div>
</div>

<svelte:window onkeydown={handleFullscreenKey} />
