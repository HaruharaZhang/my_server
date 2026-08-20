import { mkdir, readFile, readdir, rename, rm, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import sharp from 'sharp';

const projectRoot = process.cwd();
const portfolioRoot = path.join(projectRoot, 'public/images/portfolio');
const webRoot = path.join(portfolioRoot, 'web');
const dataFile = path.join(projectRoot, 'src/data/portfolio-images.json');

const categories = [
	{ directory: 'ME！', prefix: 'ME', category: 'me', legacyPrefixes: ['self', 'ME'] },
	{ directory: 'together～', prefix: 'together', category: 'together', legacyPrefixes: ['together'] },
	{ directory: 'worlds', prefix: 'worlds', category: 'worlds', legacyPrefixes: ['world', 'worlds'] },
	{ directory: 'friends', prefix: 'friends', category: 'friends', legacyPrefixes: ['portrait', 'portraits', 'friends'] },
];

const imagePattern = /\.(avif|jpe?g|png|webp)$/i;
const previousData = JSON.parse(await readFile(dataFile, 'utf8'));
const previousByFilename = new Map(
	previousData.map((image) => [path.basename(image.src), image]),
);
const previousByStem = new Map(
	previousData.map((image) => [path.parse(path.basename(image.src)).name.toLowerCase(), image]),
);
const previousByRank = new Map(
	previousData.map((image) => [
		`${image.category === 'self' ? 'me' : image.category}:${image.sourceRank}`,
		image,
	]),
);

const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

const normalizedIndex = (filename, category) => {
	const stem = path.parse(filename).name;
	const prefixes = category.legacyPrefixes.map(escapeRegExp).join('|');
	const match = stem.match(new RegExp(`^(?:${prefixes})-(\\d+)$`, 'i'));
	return match ? Number(match[1]) : null;
};

const captureTimestamp = (filename) => {
	const vrchat = filename.match(/VRChat_(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})/i);
	if (vrchat) {
		const [, year, month, day, hour, minute, second] = vrchat;
		return Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second));
	}
	const compact = filename.match(/(?:^|[^\d])(20\d{2})(\d{2})(\d{2})(?:[^\d]|$)/);
	if (compact) {
		const [, year, month, day] = compact;
		return Date.UTC(Number(year), Number(month) - 1, Number(day));
	}
	return null;
};

const displayDate = async (item) => {
	const previous = previousByFilename.get(item.originalName)
		?? previousByStem.get(path.parse(item.originalName).name.toLowerCase())
		?? previousByRank.get(`${item.category.category}:${Number(item.number)}`);
	if (previous?.date) return previous.date;
	const timestamp = captureTimestamp(item.originalName);
	const date = timestamp === null ? (await stat(item.sourcePath)).birthtime : new Date(timestamp);
	return [date.getUTCFullYear(), String(date.getUTCMonth() + 1).padStart(2, '0'), String(date.getUTCDate()).padStart(2, '0')].join('/');
};

const inventories = [];
for (const category of categories) {
	const directoryPath = path.join(portfolioRoot, category.directory);
	const filenames = (await readdir(directoryPath)).filter((filename) => imagePattern.test(filename));
	const items = filenames.map((filename) => {
		const index = normalizedIndex(filename, category);
		return {
			originalName: filename,
			sourcePath: path.join(directoryPath, filename),
			normalizedIndex: index,
			captureTimestamp: captureTimestamp(filename),
		};
	});

	items.sort((left, right) => {
		const leftNormalized = left.normalizedIndex !== null;
		const rightNormalized = right.normalizedIndex !== null;
		if (leftNormalized !== rightNormalized) return leftNormalized ? -1 : 1;
		if (leftNormalized && rightNormalized) return left.normalizedIndex - right.normalizedIndex;
		if (left.captureTimestamp !== null && right.captureTimestamp !== null) return left.captureTimestamp - right.captureTimestamp;
		if (left.captureTimestamp !== null) return -1;
		if (right.captureTimestamp !== null) return 1;
		return left.originalName.localeCompare(right.originalName, 'en', { numeric: true });
	});

	const digits = Math.max(3, String(items.length).length);
	items.forEach((item, index) => {
		const number = String(index + 1).padStart(digits, '0');
		const extension = path.extname(item.originalName).toLowerCase();
		item.number = number;
		item.finalName = `${category.prefix}-${number}${extension}`;
		item.finalPath = path.join(directoryPath, item.finalName);
		item.category = category;
	});
	inventories.push(...items);
}

const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
const backupDirectory = path.join(projectRoot, 'outputs', `portfolio-sync-${timestamp}`);
await mkdir(backupDirectory, { recursive: true });
await writeFile(path.join(backupDirectory, 'portfolio-images.before.json'), `${JSON.stringify(previousData, null, 2)}\n`);
await writeFile(
	path.join(backupDirectory, 'rename-map.json'),
	`${JSON.stringify(inventories.map((item) => ({
		category: item.category.category,
		from: path.relative(projectRoot, item.sourcePath),
		to: path.relative(projectRoot, item.finalPath),
	})), null, 2)}\n`,
);

const renamePlans = inventories.filter((item) => item.sourcePath !== item.finalPath);
for (const [index, item] of renamePlans.entries()) {
	const extension = path.extname(item.originalName).toLowerCase();
	item.temporaryPath = path.join(path.dirname(item.sourcePath), `.codex-portfolio-${timestamp}-${index}${extension}`);
	await rename(item.sourcePath, item.temporaryPath);
}
for (const item of renamePlans) {
	await rename(item.temporaryPath, item.finalPath);
	item.sourcePath = item.finalPath;
}

const runPool = async (items, concurrency, worker) => {
	let cursor = 0;
	const runners = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
		while (cursor < items.length) {
			const item = items[cursor];
			cursor += 1;
			await worker(item);
		}
	});
	await Promise.all(runners);
};

await rm(webRoot, { recursive: true, force: true });
await runPool(inventories, 4, async (item) => {
	const outputDirectory = path.join(webRoot, item.category.category);
	await mkdir(outputDirectory, { recursive: true });
	const outputName = `${item.category.prefix}-${item.number}.webp`;
	item.outputPath = path.join(outputDirectory, outputName);
	item.outputSrc = `/images/portfolio/web/${item.category.category}/${outputName}`;
	await sharp(item.sourcePath)
		.rotate()
		.resize({ width: 1920, height: 1920, fit: 'inside', withoutEnlargement: true })
		.webp({ quality: 84, effort: 4 })
		.toFile(item.outputPath);
	const metadata = await sharp(item.outputPath).metadata();
	item.width = metadata.width;
	item.height = metadata.height;
});

const portfolioImages = [];
for (const item of inventories) {
	portfolioImages.push({
		id: `${item.category.category}-${item.number}`,
		category: item.category.category,
		src: item.outputSrc,
		width: item.width,
		height: item.height,
		title: path.parse(item.finalName).name,
		date: await displayDate(item),
		sourceRank: Number(item.number),
	});
}

await writeFile(dataFile, `${JSON.stringify(portfolioImages, null, 2)}\n`);

const summary = Object.fromEntries(categories.map((category) => [
	category.category,
	portfolioImages.filter((image) => image.category === category.category).length,
]));
console.log(JSON.stringify({ summary, backupDirectory: path.relative(projectRoot, backupDirectory) }, null, 2));
