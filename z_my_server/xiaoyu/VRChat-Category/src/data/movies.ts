import douyinMovies from './douyin-movies.json';
import movieCuration from './movie-curation.json';
import { sitePath } from '../utils/site-path';

export type MovieCategory = 'daily' | 'tutorial' | 'dance';
export type MoviePlatform = 'douyin' | 'bilibili';

export interface MovieEntry {
	index: string;
	title: string;
	duration: string;
	category: MovieCategory;
	format: 'vertical' | 'video';
	ratio: number;
	aspectLabel: string;
	resolution: string;
	thumbnail: string;
	embedUrl: string;
	platforms: Array<{
		id: MoviePlatform;
		label: string;
		url: string;
	}>;
}

type CuratedMovieEntry = Omit<MovieEntry, 'index'> & { key: string };

const deletedMovieKeys = new Set(movieCuration.deleted);
const categoryOverrides = movieCuration.categories as Record<string, MovieCategory>;

const bilibiliMovies = [
	{
		title: 'VRC地图制作 拥有自己的小房间第一期：找个毛坯房上传',
		url: 'https://www.bilibili.com/video/BV1Y5KsewEaK',
		duration: '02:18',
		thumbnail: 'https://i2.hdslb.com/bfs/archive/2cf9ddbab04cb8bdcb7a92cc6243008683a621ae.jpg',
		resolution: '1920 × 1080',
	},
	{
		title: 'VRC地图制作 拥有自己的小房间第二期：添加视频播放器',
		url: 'https://www.bilibili.com/video/BV15AKFeYEmm',
		duration: '02:44',
		thumbnail: 'https://i0.hdslb.com/bfs/archive/7036d0ca612b02d84b12bb7c837b01667519b234.jpg',
		resolution: '1920 × 1080',
	},
	{
		title: 'VRC地图制作 拥有自己的小房间第三期：添加笔和镜子',
		url: 'https://www.bilibili.com/video/BV1FmKKeoEGD',
		duration: '02:15',
		thumbnail: 'https://i2.hdslb.com/bfs/archive/4c62eab15ba7a8ab96bcc5b8d2a1edc4a3e5175d.jpg',
		resolution: '1920 × 1080',
	},
	{
		title: 'VRC地图制作 拥有自己的小房间第四期：添加照片',
		url: 'https://www.bilibili.com/video/BV1m2ATevEmg',
		duration: '02:49',
		thumbnail: 'https://i1.hdslb.com/bfs/archive/6a1c5352cb291c93c3112ac91f4c45af03d3bcf6.jpg',
		resolution: '1920 × 1080',
	},
	{
		title: 'VRC地图制作 拥有自己的小房间第五期：物品的开关',
		url: 'https://www.bilibili.com/video/BV164AaexEig',
		duration: '02:26',
		thumbnail: 'https://i1.hdslb.com/bfs/archive/538698e666aba388e369126076a10ddbb27d2775.jpg',
		resolution: '1920 × 1080',
	},
	{
		title: 'VRC地图制作 拥有自己的小房间第六期：碰撞箱 能坐的椅子 透明材质',
		url: 'https://www.bilibili.com/video/BV1eCApeXEqR',
		duration: '02:42',
		thumbnail: 'https://i1.hdslb.com/bfs/archive/dff762afb0ef02c042744b92c323bede0f44cb48.jpg',
		resolution: '1920 × 1080',
	},
	{
		title: 'VRC地图制作 拥有自己的小房间第6.5期：进退房间音效',
		url: 'https://www.bilibili.com/video/BV1vCPMenEBK',
		duration: '01:57',
		thumbnail: 'https://i0.hdslb.com/bfs/archive/74458078ad64c66840df8723aa8283b949c9d4b8.jpg',
		resolution: '1920 × 1080',
	},
	{
		title: 'VRC地图制作 拥有自己的小房间第七期：AudioLink',
		url: 'https://www.bilibili.com/video/BV1FmA9eHEG6',
		duration: '02:54',
		thumbnail: 'https://i0.hdslb.com/bfs/archive/ec39ef15a0572e9a7d99322e833526082e82fcbe.jpg',
		resolution: '1920 × 1080',
	},
	{
		title: 'VRC地图制作 拥有自己的小房间第八期：物品拾取',
		url: 'https://www.bilibili.com/video/BV1MD9KYrEE8',
		duration: '03:24',
		thumbnail: 'https://i1.hdslb.com/bfs/archive/018915c25a0d96d9f5a59d978c0ca6fcb93cee0b.jpg',
		resolution: '1920 × 1080',
	},
	{
		title: 'VRC地图制作 拥有自己的小房间第九期：后处理效果 玩家速度 重力等',
		url: 'https://www.bilibili.com/video/BV18zXEYCEmF',
		duration: '02:07',
		thumbnail: 'https://i2.hdslb.com/bfs/archive/a30d7aa4ec6080f8365d3f8841885d23843434a7.jpg',
		resolution: '1920 × 1080',
	},
	{
		title: 'VRC地图制作 拥有自己的小房间第十期：将角色模型添加到地图世界中',
		url: 'https://www.bilibili.com/video/BV1nGd2Y2Ey6',
		duration: '03:02',
		thumbnail: 'https://i2.hdslb.com/bfs/archive/f0268eb660f61337be6825d825398fc771e74da8.jpg',
		resolution: '1920 × 1080',
	},
	{
		title: 'VRC地图制作 拥有自己的小房间第11期：天空盒替换',
		url: 'https://www.bilibili.com/video/BV1pGMAznEff',
		duration: '01:54',
		thumbnail: 'https://i2.hdslb.com/bfs/archive/cf9da56926f22304059a59a727e9299c6dab8640.jpg',
		resolution: '1280 × 720',
	},
	{
		title: 'VRC地图制作 拥有自己的小房间第12期：怎么做自己的小模型房',
		url: 'https://www.bilibili.com/video/BV1tpbCztEEs',
		duration: '02:08',
		thumbnail: 'https://i1.hdslb.com/bfs/archive/74d2526d503e72575f997b07e9ffb1ab1c2cb629.jpg',
		resolution: '1920 × 1080',
	},
	{
		title: 'VRC地图制作 拥有自己的小房间第13期：体积光 / VRC Light Volume',
		url: 'https://www.bilibili.com/video/BV1M5tdzbEZA',
		duration: '03:46',
		thumbnail: 'https://i1.hdslb.com/bfs/archive/af7c3c4b68327d22beba95f9884d521a9bf945de.jpg',
		resolution: '1920 × 1080',
	},
] as const;

const bilibiliEntries: CuratedMovieEntry[] = bilibiliMovies.map((movie, index) => {
	const bvid = movie.url.match(/\/video\/([^/?]+)/)?.[1] ?? '';

	return {
		key: `bilibili:${bvid}`,
		title: movie.title,
		duration: movie.duration,
		category: 'tutorial',
		format: 'video',
		ratio: 16 / 9,
		aspectLabel: '16:9',
		resolution: movie.resolution,
		thumbnail: sitePath(`images/movies/${String(index + 1).padStart(2, '0')}.jpg`),
		embedUrl: `https://player.bilibili.com/player.html?bvid=${bvid}&page=1&high_quality=1&danmaku=0&autoplay=1`,
		platforms: [{ id: 'bilibili', label: 'BILI', url: movie.url }],
	};
});

const classifyDouyinMovie = (text: string): MovieCategory => {
	const normalized = text.toLowerCase();
	if (/(舞蹈|宅舞|跳舞|编舞|舞房|dance|hip\s?hop|mood)/u.test(normalized)) return 'dance';
	if (/(教程|教学|技巧|攻略|摄影|拍照|地图制作|上传|unity|blender|shader|改模|模型制作|新手|设置)/u.test(normalized)) return 'tutorial';
	return 'daily';
};

const formatDuration = (milliseconds: number) => {
	const seconds = Math.max(0, Math.round(milliseconds / 1000));
	const minutes = Math.floor(seconds / 60);
	return `${String(minutes).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
};

const douyinEntries: CuratedMovieEntry[] = douyinMovies.map((movie) => {
	const ratio = movie.width / movie.height;
	const vertical = ratio < 0.8;
	const aspectLabel = vertical ? '9:16' : ratio > 1.25 ? '16:9' : '1:1';

	return {
		key: `douyin:${movie.id}`,
		title: movie.title,
		duration: formatDuration(movie.durationMs),
		category: classifyDouyinMovie(movie.description),
		format: vertical ? 'vertical' : 'video',
		ratio,
		aspectLabel,
		resolution: `${movie.width} × ${movie.height}`,
		thumbnail: sitePath(`images/movies/douyin/${movie.id}.jpg`),
		embedUrl: `https://open.douyin.com/player/video?vid=${movie.id}&autoplay=1`,
		platforms: [{ id: 'douyin', label: 'DY', url: `https://www.douyin.com/video/${movie.id}` }],
	};
});

export const movies: MovieEntry[] = [...douyinEntries, ...bilibiliEntries]
	.filter((movie) => !deletedMovieKeys.has(movie.key))
	.map(({ key, ...movie }, index) => ({
		...movie,
		category: categoryOverrides[key] ?? movie.category,
		index: String(index + 1).padStart(2, '0'),
	}));

export const movieCategories = [
	{ id: 'daily', label: 'DAILY' },
	{ id: 'tutorial', label: 'TUTORIAL' },
	{ id: 'dance', label: 'DANCE' },
] as const;
