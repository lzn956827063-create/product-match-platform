"""Content-addressed admission of the full inference configuration."""
import json
from pathlib import Path
from .normalize import RULE_VERSION, digest

RETRIEVAL = {'version': 'char-tfidf-2-4-v2', 'candidate_budget': 20, 'ngram_range': [2, 4], 'max_features': 150000, 'minimum_similarity': .08, 'model_boost': .25}


def artifact_path(artifact_id):
    from packages.domain.config import MODEL_DIR
    root = MODEL_DIR.resolve()
    path = (root / artifact_id).resolve()
    if not path.is_relative_to(root) or Path(artifact_id).is_absolute() or not path.is_file():
        raise ValueError('ARTIFACT_UNAVAILABLE')
    return path


def policy_hash(config):
    return digest({k: v for k, v in config.items() if k not in ('bundle_hash', 'validated', 'admission')})


def validate_bundle(config):
    from .engine import FEATURE_NAMES, FEATURE_VERSION
    if config.get('engine') != 'lightgbm' or config.get('bundle_hash') != policy_hash(config):
        raise ValueError('POLICY_HASH_MISMATCH')
    if config.get('rule_version') != RULE_VERSION or config.get('feature_schema') != FEATURE_VERSION or config.get('feature_names') != FEATURE_NAMES:
        raise ValueError('FEATURE_SCHEMA_MISMATCH')
    if config.get('normalizer_hash') != digest(Path(__file__).with_name('normalize.py').read_bytes()) or config.get('scoring_code_hash') != digest(Path(__file__).with_name('engine.py').read_bytes()):
        raise ValueError('PIPELINE_CODE_MISMATCH')
    if config.get('retrieval') != RETRIEVAL or config.get('category') != 'phone':
        raise ValueError('RETRIEVAL_OR_CATEGORY_MISMATCH')
    for kind in ('model', 'vectorizer'):
        path = artifact_path(config[kind+'_artifact'])
        if digest(path.read_bytes()) != config[kind+'_hash']:
            raise ValueError(kind.upper()+'_HASH_MISMATCH')
    if not (0 <= config['high_threshold'] <= 1 and 0 <= config['margin'] <= 1):
        raise ValueError('INVALID_THRESHOLDS')
    calibration = config.get('calibration')
    if calibration is not None and (set(calibration) != {'coef','intercept'} or not all(__import__('math').isfinite(v) for v in calibration.values())):
        raise ValueError('INVALID_CALIBRATION')
    return config


def admission(report, config):
    """Trial eligibility is distinct from a stable 98% quality claim."""
    validate_bundle(config)
    reasons = []
    if report.get('policy_hash') != policy_hash(config): reasons.append('POLICY_REPORT_MISMATCH')
    if report.get('dataset_hash') != config.get('evaluation_dataset_hash') or report.get('dataset_version') != config.get('evaluation_dataset_version'): reasons.append('DATASET_REPORT_MISMATCH')
    if not report.get('frozen_test_eligible'): reasons.append('INDEPENDENT_HUMAN_LABELS_REQUIRED')
    m = report.get('metrics', {})
    for field, target, operator in [('recall_at_20', .95, 'min'), ('top1_precision', .98, 'min'), ('coverage', .30, 'min'), ('no_match_false_recommendation', .01, 'max')]:
        value = m.get(field)
        if value is None or (value < target if operator == 'min' else value > target): reasons.append('METRIC_'+field.upper())
    if m.get('recommended_entities', 0) < 200: reasons.append('INDEPENDENT_ENTITY_COUNT')
    interval = m.get('top1_precision_95_ci')
    return {'eligible': not reasons, 'reasons': reasons, 'stable_98_claim': not reasons and bool(interval and interval[0] >= .98), 'mode': 'human_review_required'}


def make_bundle(model_dir, dataset_hash, dataset_version, high_threshold=.98, margin=.08):
    from .engine import DEFAULT_POLICY, FEATURE_NAMES
    from packages.domain.config import ROOT
    directory = Path(model_dir).resolve()
    metrics = json.loads((directory/'metrics.json').read_text())
    config = {**DEFAULT_POLICY, 'engine': 'lightgbm', 'feature_names': FEATURE_NAMES, 'normalizer_hash':digest(Path(__file__).with_name('normalize.py').read_bytes()), 'scoring_code_hash':digest(Path(__file__).with_name('engine.py').read_bytes()), 'retrieval': RETRIEVAL, 'high_threshold': high_threshold, 'margin': margin, 'calibration': metrics.get('calibration'), 'evaluation_dataset_hash': dataset_hash, 'evaluation_dataset_version': dataset_version}
    for kind, filename in [('model','model.txt'),('vectorizer','tfidf.joblib')]:
        path = directory/filename
        config[kind+'_artifact'] = str(path.relative_to(ROOT/'models'))
        config[kind+'_hash'] = digest(path.read_bytes())
    config['bundle_hash'] = policy_hash(config)
    validate_bundle(config)
    return config
