"""Check raw recommendation preferences stay outside model request payloads."""
from rynmesh.recommendation_profile import RecommendationProfileStore
from rynmesh.services.digest import DigestService


def test_enrichment_sends_public_metadata_without_profile_or_feedback_records(tmp_path):
    profile = RecommendationProfileStore(tmp_path / 'profile.json')
    profile.patch({'direction': 'PRIVATE_DIRECTION_CANARY_48'})
    service = DigestService(tmp_path / 'digest', profile_store=profile)
    requested = []

    def fetch(url, *_):
        requested.append(url)
        return b'<rss version="2.0"><channel><title>Public source</title><item><title>Public article</title><link>https://example.test/article</link><description>Public metadata only.</description></item></channel></rss>'

    service.fetcher = fetch
    service.add_source('https://example.test/feed')
    service.refresh()
    service.build(now_unix=1800000000)
    item = service.recommendation_items()[0]
    profile.feedback({**item, 'title': 'PRIVATE_FEEDBACK_TITLE_CANARY_48',
        'tags': ['PRIVATE_SIGNAL_CANARY_48']}, 'more')
    event_id = profile.history()['items'][0]['event_id']

    class Recorder:
        id = 'acceptance-recorder'
        model = 'no-real-model'

        def __init__(self):
            self.prompts = []

        def generate(self, prompt, **kwargs):
            self.prompts.append(prompt)
            return 'Public metadata summary.'

    provider = Recorder()
    result = service.build(now_unix=1800000000, provider=provider)
    assert result['items'] and len(provider.prompts) == 2
    combined = '\n'.join(provider.prompts)
    assert 'Public article' in combined and 'Public metadata only.' in combined
    for private in ('PRIVATE_DIRECTION_CANARY_48', 'PRIVATE_SIGNAL_CANARY_48',
                    'PRIVATE_FEEDBACK_TITLE_CANARY_48', event_id):
        assert private not in combined and all(private not in url for url in requested)
    assert profile.history()['total'] == 1
