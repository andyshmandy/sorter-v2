// Whether this browser tab is showing. Camera feeds stop while it is hidden:
// an open MJPEG stream costs the machine bandwidth and holds one of the six
// connections a browser allows to it.
let visible = $state(typeof document === 'undefined' || !document.hidden);
if (typeof document !== 'undefined') {
	document.addEventListener('visibilitychange', () => (visible = !document.hidden));
}

export const tab = {
	get visible() {
		return visible;
	}
};
