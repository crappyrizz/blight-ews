// The data the app shows, parsed from the backend's JSON.
//
// Parsing is forgiving: a field the backend leaves out or sets to null
// becomes "not known" in the app, never a crash. Missing data is shown as
// missing, never as "low risk" (the project's missing-data rule).

class Farmer {
  Farmer({required this.farmerId, required this.name, required this.phoneNumber, this.farmId});

  final String farmerId;
  final String name;
  final String phoneNumber;
  final String? farmId;

  factory Farmer.fromJson(Map<String, dynamic> j) => Farmer(
        farmerId: j['farmer_id'] as String,
        name: j['name'] as String,
        phoneNumber: j['phone_number'] as String,
        farmId: j['farm_id'] as String?,
      );
}

class SensorNode {
  SensorNode({required this.nodeId, required this.location});

  final String nodeId;
  final String location;

  factory SensorNode.fromJson(Map<String, dynamic> j) =>
      SensorNode(nodeId: j['node_id'] as String, location: (j['location'] as String?) ?? 'My plot');
}

/// low / medium / high, or unknown when there is no recent judged data.
enum RiskLevel { low, medium, high, unknown }

RiskLevel parseRiskLevel(String? value) => switch (value) {
      'low' => RiskLevel.low,
      'medium' => RiskLevel.medium,
      'high' => RiskLevel.high,
      _ => RiskLevel.unknown,
    };

/// Words for the farmer. "medium" is shown as MODERATE, as in the wireframe.
String riskLabel(RiskLevel level) => switch (level) {
      RiskLevel.low => 'LOW RISK',
      RiskLevel.medium => 'MODERATE RISK',
      RiskLevel.high => 'HIGH RISK',
      RiskLevel.unknown => 'RISK UNKNOWN',
    };

/// "just now", "12 min ago", "3 hours ago", "2 days ago".
String ageText(int? minutes) {
  if (minutes == null) return 'no readings yet';
  if (minutes < 2) return 'just now';
  if (minutes < 60) return '$minutes min ago';
  final hours = minutes ~/ 60;
  if (hours < 48) return '$hours ${hours == 1 ? 'hour' : 'hours'} ago';
  return '${hours ~/ 24} days ago';
}

class LatestReading {
  LatestReading({required this.temperature, required this.humidity, required this.ageMinutes});

  final double temperature;
  final double humidity;
  final int ageMinutes;

  static LatestReading? fromJson(dynamic j) => j is Map<String, dynamic>
      ? LatestReading(
          temperature: (j['temperature'] as num).toDouble(),
          humidity: (j['humidity'] as num).toDouble(),
          ageMinutes: (j['age_minutes'] as num).toInt(),
        )
      : null;
}

class HuttonToday {
  HuttonToday({required this.status, required this.label, this.huttonDay, this.huttonPeriod});

  final String status; // ok | insufficient_data | no_data | not_computed
  final String label; // e.g. "Incomplete data (14 of 24 hours)", ready to show
  final bool? huttonDay;
  final bool? huttonPeriod;

  factory HuttonToday.fromJson(Map<String, dynamic>? j) => HuttonToday(
        status: (j?['status'] as String?) ?? 'not_computed',
        label: (j?['label'] as String?) ?? 'Not computed yet',
        huttonDay: j?['hutton_day'] as bool?,
        huttonPeriod: j?['hutton_period'] as bool?,
      );

  /// One plain line about today's weather against the Hutton rule.
  String get explanation {
    if (status != 'ok') return label; // the backend already says what is missing
    if (huttonPeriod == true) return 'Blight weather two days running (Hutton period)';
    if (huttonDay == true) return 'Blight weather today (Hutton day)';
    return 'Today has not had blight weather';
  }
}

class Outlook {
  Outlook({required this.probability, required this.level, required this.issuedFrom, required this.covers});

  final double probability;
  final RiskLevel level;
  final DateTime? issuedFrom; // the day whose readings made the forecast
  final List<DateTime> covers; // the days it is about

  static Outlook? fromJson(dynamic j) => j is Map<String, dynamic>
      ? Outlook(
          probability: (j['probability'] as num).toDouble(),
          level: parseRiskLevel(j['risk_level'] as String?),
          issuedFrom: DateTime.tryParse((j['issued_from'] ?? '').toString()),
          covers: ((j['covers'] as List?) ?? const [])
              .map((d) => DateTime.tryParse(d.toString()))
              .whereType<DateTime>()
              .toList(),
        )
      : null;

  /// Still about today or later? A forecast for days already past must not
  /// be shown as "next 24 hours" (e.g. when the sensor has gone quiet).
  bool isCurrent(DateTime today) {
    if (covers.isEmpty) return false;
    final day = DateTime(today.year, today.month, today.day);
    return !covers.last.isBefore(day);
  }
}

const _months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/// "11 Oct".
String shortDate(DateTime d) => '${d.day} ${_months[d.month - 1]}';

class Dashboard {
  Dashboard({
    required this.level,
    required this.score,
    required this.riskNote,
    required this.summary,
    required this.latest,
    required this.huttonToday,
    required this.outlook24,
    required this.outlook48,
  });

  final RiskLevel level;
  final double? score;
  final String riskNote;
  final String summary;
  final LatestReading? latest;
  final HuttonToday huttonToday;
  final Outlook? outlook24;
  final Outlook? outlook48;

  factory Dashboard.fromJson(Map<String, dynamic> j) {
    final risk = (j['risk'] as Map<String, dynamic>?) ?? const {};
    final forecast = (j['forecast'] as Map<String, dynamic>?) ?? const {};
    // The neutral prior (no recent judged data) is "unknown", not "moderate".
    final unknown = risk['basis'] == 'neutral_prior';
    return Dashboard(
      level: unknown ? RiskLevel.unknown : parseRiskLevel(risk['risk_level'] as String?),
      score: unknown ? null : (risk['score'] as num?)?.toDouble(),
      riskNote: (risk['note'] as String?) ?? '',
      summary: (j['summary'] as String?) ?? '',
      latest: LatestReading.fromJson(j['latest_reading']),
      huttonToday: HuttonToday.fromJson(j['hutton_today'] as Map<String, dynamic>?),
      outlook24: Outlook.fromJson(forecast['p24']),
      outlook48: Outlook.fromJson(forecast['p48']),
    );
  }
}

// --- leaf photo diagnosis -------------------------------------------------------

/// The CNN's class names in the farmer's words.
String diseaseName(String label) => switch (label) {
      'late_blight' => 'Late blight',
      'early_blight' => 'Early blight',
      'healthy' => 'Healthy leaf',
      _ => label.replaceAll('_', ' '),
    };

String percent(double value) => '${(value * 100).round()}%';

class Diagnosis {
  Diagnosis({
    required this.label,
    required this.confidence,
    required this.pLate,
    required this.risk,
    required this.riskLevel,
    required this.riskBasis,
    required this.riskNote,
    required this.threshold,
    required this.flagged,
    required this.summary,
    this.smsAlert,
  });

  final String label; // the CNN's most likely class
  final double confidence; // its probability
  final double pLate; // the CNN's late-blight probability
  final double risk; // r, the weather risk used
  final RiskLevel riskLevel;
  final String riskBasis; // learned_24h | hutton_today | hutton_fallback | neutral_prior
  final String riskNote;
  final double threshold; // tau(r)
  final bool flagged; // pLate >= tau(r)
  final String summary;
  final String? smsAlert; // delivery status if an SMS went out

  /// Where the weather risk came from, in words.
  String get riskSource => switch (riskBasis) {
        'learned_24h' => "the forecast made from your sensor's readings",
        'hutton_today' => "today's sensor readings",
        'hutton_fallback' => 'your sensor readings from a recent day',
        _ => 'no recent weather readings, so a neutral value was used',
      };

  /// The reasoning, expanded on tap: how the weather moved the bar.
  String get reasoning {
    const neutral = 0.60; // TAU0 in backend/fusion.py: the bar when the risk is neutral
    final String direction;
    if (threshold < neutral - 0.005) {
      direction = 'lowered the bar from ${percent(neutral)} to ${percent(threshold)}, '
          'because the weather favours blight';
    } else if (threshold > neutral + 0.005) {
      direction = 'raised the bar from ${percent(neutral)} to ${percent(threshold)}, '
          'because the weather does not favour blight';
    } else {
      direction = 'kept the bar at ${percent(threshold)}';
    }
    final verdict = flagged
        ? 'The photo scored ${percent(pLate)} for late blight, which reaches the bar, so it is flagged.'
        : 'The photo scored ${percent(pLate)} for late blight, which is below the bar, so it is not flagged.';
    return 'Weather risk: ${percent(risk)}, from $riskSource. '
        'To flag late blight the app $direction. $verdict';
  }

  factory Diagnosis.fromJson(Map<String, dynamic> d, Map<String, dynamic> prediction) {
    final probabilities = (prediction['probabilities'] as Map?) ?? const {};
    return Diagnosis(
      label: d['label'] as String,
      confidence: (d['confidence'] as num).toDouble(),
      pLate: ((probabilities['late_blight'] as num?) ?? 0).toDouble(),
      risk: (d['risk_score_used'] as num).toDouble(),
      riskLevel: d['risk_basis'] == 'neutral_prior'
          ? RiskLevel.unknown
          : parseRiskLevel(d['risk_level'] as String?),
      riskBasis: (d['risk_basis'] as String?) ?? '',
      riskNote: (d['risk_note'] as String?) ?? '',
      threshold: (d['threshold_used'] as num).toDouble(),
      flagged: d['late_blight_flagged'] as bool,
      summary: (d['summary'] as String?) ?? '',
      smsAlert: d['sms_alert'] as String?,
    );
  }
}

class UploadResult {
  UploadResult({required this.imageId, required this.diagnosis});

  final String imageId;
  final Diagnosis diagnosis;

  factory UploadResult.fromJson(Map<String, dynamic> j) => UploadResult(
        imageId: (j['image'] as Map<String, dynamic>)['image_id'] as String,
        diagnosis: Diagnosis.fromJson(
          j['diagnosis'] as Map<String, dynamic>,
          j['prediction'] as Map<String, dynamic>,
        ),
      );
}

// --- history (FR viii) -------------------------------------------------------------

enum HistoryKind { diagnosis, alert, risk }

/// One line in the history list.
class HistoryItem {
  HistoryItem({required this.kind, required this.when, required this.title, required this.detail, this.level});

  final HistoryKind kind;
  final DateTime when; // local time; for a day's weather risk, the end of that day
  final String title;
  final String detail;
  final RiskLevel? level;
}

class History {
  History(this.items);

  /// Newest first. Under each day, that day's weather risk comes first (it is
  /// dated at the end of the day), then the day's photos and SMS, newest first.
  final List<HistoryItem> items;

  factory History.fromJson(Map<String, dynamic> j) {
    final items = <HistoryItem>[];
    for (final d in (j['diagnoses'] as List?) ?? const []) {
      final m = d as Map<String, dynamic>;
      final flagged = m['late_blight_flagged'] == true;
      items.add(HistoryItem(
        kind: HistoryKind.diagnosis,
        when: DateTime.parse(m['timestamp'] as String).toLocal(),
        title: 'Leaf photo: ${diseaseName(m['disease_label'] as String)} '
            '(${percent((m['confidence'] as num).toDouble())})',
        detail: flagged ? 'Late blight flagged' : 'Late blight not flagged',
        level: parseRiskLevel(m['risk_level'] as String?),
      ));
    }
    for (final a in (j['alerts'] as List?) ?? const []) {
      final m = a as Map<String, dynamic>;
      items.add(HistoryItem(
        kind: HistoryKind.alert,
        when: DateTime.parse(m['sent_at'] as String).toLocal(),
        title: switch (m['alert_type']) {
          'high_risk' => 'SMS: high risk warning',
          'diagnosis' => 'SMS: leaf photo result',
          _ => 'SMS: status',
        },
        detail: (m['message'] as String?) ?? '',
      ));
    }
    for (final r in (j['risk'] as List?) ?? const []) {
      final m = r as Map<String, dynamic>;
      final day = DateTime.parse(m['for_date'] as String);
      final sensor = (m['hutton'] as Map?)?['sensor'] as Map?;
      final level = m['risk_level'] == null ? RiskLevel.unknown : parseRiskLevel(m['risk_level'] as String?);
      items.add(HistoryItem(
        kind: HistoryKind.risk,
        when: DateTime(day.year, day.month, day.day, 23, 59),
        title: level == RiskLevel.unknown
            ? 'Weather risk: not known'
            : 'Weather risk: ${riskLabel(level).replaceAll(' RISK', '').toLowerCase()}',
        detail: (sensor?['label'] as String?) ?? 'No sensor readings',
        level: level,
      ));
    }
    items.sort((a, b) => b.when.compareTo(a.when));
    return History(items);
  }
}

// --- symptom onset reports ------------------------------------------------------------

class SymptomReport {
  SymptomReport({required this.onsetDate, this.notes});

  final DateTime onsetDate;
  final String? notes;

  factory SymptomReport.fromJson(Map<String, dynamic> j) =>
      SymptomReport(onsetDate: DateTime.parse(j['onset_date'] as String), notes: j['notes'] as String?);
}

const _weekdays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/// "Mon 12 Oct".
String dayHeading(DateTime d) => '${_weekdays[d.weekday - 1]} ${shortDate(d)}';

/// "14:05".
String clockTime(DateTime d) => '${d.hour.toString().padLeft(2, '0')}:${d.minute.toString().padLeft(2, '0')}';
