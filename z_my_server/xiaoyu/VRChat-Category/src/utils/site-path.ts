const base = import.meta.env.BASE_URL.replace(/\/$/, '');

export const sitePath = (path = '') => `${base}/${path.replace(/^\//, '')}`;
