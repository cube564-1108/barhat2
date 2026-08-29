<?php
/**
 * Пост-обработчик выгрузки каталога для RetailCRM.
 *
 * ЗАЧЕМ. Модуль intaro.retailcrm берёт ссылку на товар готовой у Битрикса
 * (lib/icml/xmlofferbuilder.php: $item['DETAIL_PAGE_URL'] . домен) и выбирает
 * товары запросом БЕЗ фильтра по разделу. Битрикс на такой запрос отдаёт по
 * строке на каждую привязку элемента к разделу, модуль берёт первую — то есть
 * привязку, созданную раньше прочих. Основной раздел товара в этом не участвует.
 *
 * Замер 2026-08-28 по всем 456 товарам: у 147 ссылка нерабочая. Если первой
 * привязкой стоит навигационный раздел («Все букеты», «Кому», «Событие») —
 * ссылка сломана в 165 случаях из 165.
 *
 * Разовая перестановка привязок не годится: порядок собьётся с первым же новым
 * товаром, а импорт из МойСклада создаёт привязки в своём порядке. Поэтому
 * чиним не данные, а выгрузку — один раз и навсегда, включая будущие товары.
 *
 * ЧТО ДЕЛАЕТ. Читает файл, сгенерированный модулем, и переписывает в каждом
 * оффере тег <url> по одному правилу:
 *
 *     ссылка выдаётся, только если у товара есть публичная страница,
 *     иначе тег остаётся пустым
 *
 * Страница есть, когда элемент активен С УЧЁТОМ ДАТ (модуль смотрит только
 * галочку «Активность» и про даты не знает — отсюда сезонные товары с битыми
 * ссылками) и раздел канонического пути активен вместе со всеми родителями
 * (GLOBAL_ACTIVE). Сам адрес берётся по ОСНОВНОМУ разделу элемента — тому,
 * по которому сайт строит <link rel="canonical">.
 *
 * Товары без публичной страницы («Товары МС» — номенклатура из МойСклада)
 * из выгрузки НЕ убираются: они нужны в CRM для работы. У них просто не будет
 * ссылки, и админ физически не сможет отправить клиенту 404.
 *
 * КАК ЗАПУСКАТЬ. По крону, после генерации ICML (она идёт около 19:30):
 *
 *     php /home/bitrix/www/bitrix/php_interface/icml_fix_urls.php
 *
 * Затем в RetailCRM (Администрирование → Магазины) указать адрес файла,
 * который создаёт этот скрипт, — retailcrm_fixed.xml вместо retailcrm.xml.
 *
 * Модуль при этом не трогаем: его обновления ничего здесь не сломают.
 *
 * Разбор целиком: docs/инструкция-ссылки-товаров-битрикс-crm.md
 */

// Скрипт ходит по каталогу и переписывает файл — из веба его дёргать незачем.
if (PHP_SAPI !== 'cli') {
    http_response_code(404);
    exit;
}

// --- пролог Битрикса (CLI) -------------------------------------------------
$_SERVER['DOCUMENT_ROOT'] = realpath(dirname(__FILE__) . '/../..');
define('NO_KEEP_STATISTIC', true);
define('NOT_CHECK_PERMISSIONS', true);
define('BX_CRONTAB', true);
require_once $_SERVER['DOCUMENT_ROOT'] . '/bitrix/modules/main/include/prolog_before.php';

if (!CModule::IncludeModule('iblock')) {
    fwrite(STDERR, "Не подключается модуль iblock\n");
    exit(1);
}

// --- настройки -------------------------------------------------------------

/** Файл, который генерит модуль RetailCRM. */
$sourceFile = $_SERVER['DOCUMENT_ROOT'] . '/bitrix/catalog_export/retailcrm.xml';

/** Файл с исправленными ссылками — его и указываем в настройках магазина CRM. */
$targetFile = $_SERVER['DOCUMENT_ROOT'] . '/bitrix/catalog_export/retailcrm_fixed.xml';

/** Куда писать краткий отчёт о прогоне. */
$logFile = $_SERVER['DOCUMENT_ROOT'] . '/bitrix/catalog_export/icml_fix_urls.log';

// ---------------------------------------------------------------------------

function report($message)
{
    global $logFile;
    $line = date('Y-m-d H:i:s') . '  ' . $message;
    echo $line . "\n";
    file_put_contents($logFile, $line . "\n", FILE_APPEND);
}

if (!is_readable($sourceFile)) {
    report("ОШИБКА: не читается исходный файл $sourceFile");
    exit(1);
}

// Файл старше суток — значит выгрузка перестала генерироваться. Это само по
// себе поломка: в CRM уедет вчерашний каталог, и мы об этом не узнаем.
$age = time() - filemtime($sourceFile);
if ($age > 86400 * 2) {
    report('ВНИМАНИЕ: исходный файл не обновлялся ' . round($age / 86400, 1) . ' сут.');
}

$xml = new DOMDocument();
$xml->preserveWhiteSpace = false;
if (!$xml->load($sourceFile)) {
    report("ОШИБКА: файл $sourceFile не разбирается как XML");
    exit(1);
}

$offers = $xml->getElementsByTagName('offer');
if ($offers->length === 0) {
    report('ОШИБКА: в файле нет ни одного оффера — не перезаписываю результат');
    exit(1);
}

// --- 1. собираем товары из выгрузки ----------------------------------------
// Страница есть у ТОВАРА, поэтому ориентируемся на productId: у простого товара
// он совпадает с id оффера, а у торгового предложения указывает на родителя.

$productIds = [];
foreach ($offers as $offer) {
    $productId = (int) ($offer->getAttribute('productId') ?: $offer->getAttribute('id'));
    if ($productId > 0) {
        $productIds[$productId] = true;
    }
}
$productIds = array_keys($productIds);
report('офферов в файле: ' . $offers->length . ', товаров: ' . count($productIds));

// --- 2. основной раздел каждого товара -------------------------------------

$mainSection = [];   // элемент => его основной раздел
$iblockOf = [];      // элемент => инфоблок

$rows = CIBlockElement::GetList(
    [],
    ['ID' => $productIds],
    false,
    false,
    ['ID', 'IBLOCK_ID', 'IBLOCK_SECTION_ID']
);
while ($row = $rows->Fetch()) {
    $mainSection[(int) $row['ID']] = (int) $row['IBLOCK_SECTION_ID'];
    $iblockOf[(int) $row['ID']] = (int) $row['IBLOCK_ID'];
}

// --- 3. какие разделы публичны --------------------------------------------
// GLOBAL_ACTIVE уже учитывает активность всех родителей раздела: если выключен
// сам раздел или любой из родителей, страницы товара в нём не будет.

$sectionIds = array_values(array_unique(array_filter($mainSection)));
$publicSection = [];
if ($sectionIds) {
    $sections = CIBlockSection::GetList(
        [],
        ['ID' => $sectionIds, 'GLOBAL_ACTIVE' => 'Y'],
        false,
        ['ID']
    );
    while ($section = $sections->Fetch()) {
        $publicSection[(int) $section['ID']] = true;
    }
}

// --- 4. канонический адрес по основному разделу ----------------------------
// Запрашиваем DETAIL_PAGE_URL с фильтром по конкретному разделу — тогда Битрикс
// подставляет в шаблон именно его, а не первую попавшуюся привязку.
// Группируем по разделу, чтобы не делать запрос на каждый товар отдельно.
//
// ACTIVE_DATE => 'Y' здесь ключевой: он отсеивает сезонные товары с истёкшим
// сроком активности. Не вернулся из выборки — значит публичной страницы нет.

$byGroup = [];
foreach ($mainSection as $elementId => $sectionId) {
    if ($sectionId && isset($publicSection[$sectionId])) {
        $byGroup[$iblockOf[$elementId] . ':' . $sectionId][] = $elementId;
    }
}

$canonicalUrl = [];
foreach ($byGroup as $group => $elementIds) {
    list($iblockId, $sectionId) = explode(':', $group);
    $found = CIBlockElement::GetList(
        [],
        [
            'IBLOCK_ID' => (int) $iblockId,
            'ID' => $elementIds,
            'SECTION_ID' => (int) $sectionId,
            'INCLUDE_SUBSECTIONS' => 'N',
            'ACTIVE' => 'Y',
            'ACTIVE_DATE' => 'Y',
        ],
        false,
        false,
        ['ID', 'DETAIL_PAGE_URL']
    );
    while ($item = $found->Fetch()) {
        if (!empty($item['DETAIL_PAGE_URL'])) {
            $canonicalUrl[(int) $item['ID']] = $item['DETAIL_PAGE_URL'];
        }
    }
}

// --- 5. домен ---------------------------------------------------------------
// Берём из самой выгрузки: модуль уже приклеил туда адрес сайта из своих
// настроек, и брать его же — надёжнее, чем заводить вторую копию настройки.

$serverName = '';
foreach ($offers as $offer) {
    foreach ($offer->getElementsByTagName('url') as $urlNode) {
        if (preg_match('~^(https?://[^/]+)~', trim($urlNode->nodeValue), $m)) {
            $serverName = $m[1];
            break 2;
        }
    }
}
if ($serverName === '') {
    report('ОШИБКА: не удалось определить домен по исходному файлу');
    exit(1);
}

// --- 6. переписываем ссылки -------------------------------------------------

$stats = ['исправлено' => 0, 'очищено' => 0, 'без изменений' => 0];

foreach ($offers as $offer) {
    $productId = (int) ($offer->getAttribute('productId') ?: $offer->getAttribute('id'));
    $right = isset($canonicalUrl[$productId]) ? $serverName . $canonicalUrl[$productId] : '';

    $urlNodes = $offer->getElementsByTagName('url');
    if ($urlNodes->length > 0) {
        $urlNode = $urlNodes->item(0);
    } else {
        // тега может не быть, если модуль не нашёл DETAIL_PAGE_URL
        $urlNode = $xml->createElement('url');
        $offer->appendChild($urlNode);
    }

    $was = trim($urlNode->nodeValue);
    if ($was === $right) {
        $stats['без изменений']++;
        continue;
    }

    // Пустой тег, а не удаление: так CRM видит явное «ссылки нет» и затирает
    // прежнее значение. Если окажется, что она пустой url игнорирует и держит
    // старый, — здесь нужно будет удалять узел целиком.
    $urlNode->nodeValue = '';
    if ($right !== '') {
        $urlNode->appendChild($xml->createTextNode($right));
        $stats['исправлено']++;
    } else {
        $stats['очищено']++;
    }
}

// --- 7. сохраняем атомарно --------------------------------------------------
// Через временный файл и rename: RetailCRM забирает файл по расписанию и может
// прийти ровно в момент записи — получить полфайла она не должна.

$tmpFile = $targetFile . '.tmp';
if ($xml->save($tmpFile) === false || !rename($tmpFile, $targetFile)) {
    report("ОШИБКА: не удалось записать $targetFile");
    @unlink($tmpFile);
    exit(1);
}

report(sprintf(
    'готово: ссылок исправлено %d, очищено %d, без изменений %d -> %s',
    $stats['исправлено'],
    $stats['очищено'],
    $stats['без изменений'],
    basename($targetFile)
));
