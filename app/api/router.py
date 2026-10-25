from fastapi import APIRouter

from app.api.routes import citydata, health, kakao, parking, supplemental, team_contract, traffic

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(parking.router)
router.include_router(citydata.router)

router.include_router(traffic.router)

router.include_router(kakao.router)


router.include_router(team_contract.router)


router.include_router(supplemental.router)
