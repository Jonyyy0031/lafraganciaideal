"""Wiring of the catalog module for the composition root."""

from fragancia_api.modules.catalog.application.commands.brand_status import (
    ArchiveBrand,
    RestoreBrand,
)
from fragancia_api.modules.catalog.application.commands.concentration_status import (
    ArchiveConcentration,
    RestoreConcentration,
)
from fragancia_api.modules.catalog.application.commands.create_brand import CreateBrand
from fragancia_api.modules.catalog.application.commands.create_concentration import (
    CreateConcentration,
)
from fragancia_api.modules.catalog.application.commands.create_olfactory_family import (
    CreateOlfactoryFamily,
)
from fragancia_api.modules.catalog.application.commands.olfactory_family_status import (
    ArchiveOlfactoryFamily,
    RestoreOlfactoryFamily,
)
from fragancia_api.modules.catalog.application.commands.rename_brand import RenameBrand
from fragancia_api.modules.catalog.application.commands.rename_olfactory_family import (
    RenameOlfactoryFamily,
)
from fragancia_api.modules.catalog.application.commands.update_concentration import (
    UpdateConcentration,
)
from fragancia_api.modules.catalog.application.queries.list_brands import (
    ListAdminBrands,
    ListPublicBrands,
)
from fragancia_api.modules.catalog.application.queries.list_concentrations import (
    ListAdminConcentrations,
    ListPublicConcentrations,
)
from fragancia_api.modules.catalog.application.queries.list_olfactory_families import (
    ListAdminOlfactoryFamilies,
    ListPublicOlfactoryFamilies,
)
from fragancia_api.modules.catalog.http.router import routers
from fragancia_api.modules.catalog.infrastructure.sql_brand_queries import SqlBrandQueries
from fragancia_api.modules.catalog.infrastructure.sql_brand_repository import SqlBrandRepository
from fragancia_api.modules.catalog.infrastructure.sql_concentration_queries import (
    SqlConcentrationQueries,
)
from fragancia_api.modules.catalog.infrastructure.sql_concentration_repository import (
    SqlConcentrationRepository,
)
from fragancia_api.modules.catalog.infrastructure.sql_olfactory_family_queries import (
    SqlOlfactoryFamilyQueries,
)
from fragancia_api.modules.catalog.infrastructure.sql_olfactory_family_repository import (
    SqlOlfactoryFamilyRepository,
)
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.module import AppModule, Platform


def register(platform: Platform, services: ServiceRegistry) -> None:
    brands = SqlBrandRepository(platform.database)
    queries = SqlBrandQueries(platform.database)
    services.add(
        CreateBrand,
        CreateBrand(
            brands=brands,
            transactions=platform.transactions,
            events=platform.events,
            clock=platform.clock,
        ),
    )
    services.add(RenameBrand, RenameBrand(brands=brands, transactions=platform.transactions))
    services.add(ArchiveBrand, ArchiveBrand(brands=brands, transactions=platform.transactions))
    services.add(RestoreBrand, RestoreBrand(brands=brands, transactions=platform.transactions))
    services.add(ListPublicBrands, ListPublicBrands(queries))
    services.add(ListAdminBrands, ListAdminBrands(queries))

    families = SqlOlfactoryFamilyRepository(platform.database)
    family_queries = SqlOlfactoryFamilyQueries(platform.database)
    services.add(
        CreateOlfactoryFamily,
        CreateOlfactoryFamily(
            families=families, transactions=platform.transactions, clock=platform.clock
        ),
    )
    services.add(
        RenameOlfactoryFamily,
        RenameOlfactoryFamily(families=families, transactions=platform.transactions),
    )
    services.add(
        ArchiveOlfactoryFamily,
        ArchiveOlfactoryFamily(families=families, transactions=platform.transactions),
    )
    services.add(
        RestoreOlfactoryFamily,
        RestoreOlfactoryFamily(families=families, transactions=platform.transactions),
    )
    services.add(ListPublicOlfactoryFamilies, ListPublicOlfactoryFamilies(family_queries))
    services.add(ListAdminOlfactoryFamilies, ListAdminOlfactoryFamilies(family_queries))

    concentrations = SqlConcentrationRepository(platform.database)
    concentration_queries = SqlConcentrationQueries(platform.database)
    services.add(
        CreateConcentration,
        CreateConcentration(
            concentrations=concentrations, transactions=platform.transactions, clock=platform.clock
        ),
    )
    services.add(
        UpdateConcentration,
        UpdateConcentration(concentrations=concentrations, transactions=platform.transactions),
    )
    services.add(
        ArchiveConcentration,
        ArchiveConcentration(concentrations=concentrations, transactions=platform.transactions),
    )
    services.add(
        RestoreConcentration,
        RestoreConcentration(concentrations=concentrations, transactions=platform.transactions),
    )
    services.add(ListPublicConcentrations, ListPublicConcentrations(concentration_queries))
    services.add(ListAdminConcentrations, ListAdminConcentrations(concentration_queries))


module = AppModule(name="catalog", register=register, routers=routers)
